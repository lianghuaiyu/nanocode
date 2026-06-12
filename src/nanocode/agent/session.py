"""AgentSession：state ↔ canonical 树的同步边界（docs/15 §7）。

docs/15 STEP D：从薄包装升级为「同时知道 SessionManager 与 AgentCore 的唯一对象」。
职责：
- hydrate_state()：从 canonical 树 build_context() 重建 AgentState（可丢弃投影,§6）。
- record_event(event)：把 AgentEvent 落成 canonical session entry（取代散落的 _tree_* 调用）。
- run_turn(prompt)：一个 turn 的编排外壳（当前委托 AgentCore=Agent.chat；事件 inversion 渐进上移）。
- compact() / check_and_compact()：compaction owner（docs/16 #3a）——summarizer 输入吃树渲染,
  entry 经 CompactionRequested→record_event 单写（两区 fold 的唯一 shrink 通道）。
- move_to()：in-file 树导航（移 leaf）。
- verify_turn_consistency()：turn-end 一致性检查（§7.6）——删 flat fallback 后,树是唯一权威,
  孤儿/断链/leaf 漂移必须 fail-loud 而非静默污染下一轮。

边界：AgentSession 是**唯一**调 SessionManager.append_* 的高层对象（经 agent._session_mgr）；
AgentCore 只发事件、不写 session。子 agent 暂仍走 raw Agent（Phase 6 spawn child 给其独立 AgentSession）。
"""

from __future__ import annotations

from ..session import tree as _tree
from . import events as _events
from .state import AgentState


class AgentSession:
    """会话层：拥有 agent 的 turn 生命周期、state↔tree 同步与 in-file 导航。"""

    def __init__(self, agent) -> None:
        self.agent = agent

    @property
    def session_id(self) -> str:
        return self.agent.session_id

    @property
    def aborted(self) -> bool:
        return self.agent._aborted

    # ── turn 编排 ────────────────────────────────────────────────────────────
    async def run_turn(self, prompt: str) -> None:
        """跑一个 turn：委托 AgentCore（其已含 begin_turn/user_message/模型循环/turn_end/取消语义）。
        最终文本经 sink 呈现或 run_once 的 BufferSink 捕获；结构化结果由 RuntimeThread 的 TurnResult 承载。"""
        await self.agent.chat(prompt)

    async def compact(self, instructions: str | None = None) -> None:
        """压缩当前对话（docs/16 #3a：compaction owner 上移到 turn shell）。

        summarizer 输入吃**树渲染**（hydrate_state().project()，与发给模型的上下文一致）——
        不再读 flat 列表；产出经 `CompactionRequested→record_event` 写 COMPACTION entry
        （两区 fold 的唯一 shrink 通道）。firstKept 语义保持 docs/14 §4.4 bug#1：
        cut = last_user id 仅当它 == live leaf（auto-compact 刚记完 user 消息时），否则 None。
        instructions 预留（自定义摘要指令）。"""
        a = self.agent
        mgr = a._session_mgr
        tokens_before = a.last_input_token_count
        before_count = len(mgr.build_context().messages) if mgr is not None else None
        first_kept = None
        if mgr is not None:
            leaf = mgr.get_leaf()
            last_u = mgr.last_user_message_id()
            first_kept = last_u if last_u == leaf else None
        summary = await (a._compact_openai() if a.use_openai else a._compact_anthropic())
        if summary:
            self.record_event(_events.CompactionRequested(
                reason="context_window", tokens_before=tokens_before, summary=summary,
                first_kept_entry_id=first_kept,
                message_count_before=before_count,
                message_count_after=self._predicted_post_compaction_count(summary, first_kept),
            ))
        a._sink.info("Conversation compacted.")
        a._sent_skill_names = set()  # 清单消息被压缩丢弃 → 下一轮重新播报

    async def check_and_compact(self) -> None:
        """auto-compact 阈值门（原 engine._check_and_compact，turn shell 职责）。"""
        a = self.agent
        if a.last_input_token_count > a.effective_window * 0.85:
            a._sink.info("Context window filling up, compacting conversation...")
            await self.compact()

    def _predicted_post_compaction_count(self, summary: str, first_kept: "str | None") -> "int | None":
        """预测 compaction entry 落树后的 neutral 消息数（entry 是 append-only，写后不可补字段，
        故 messageCountAfter 须在写前算）：对 branch + 合成 pending compaction entry 跑**真实** fold
        （不手写两区逻辑，与 build_context 永远一致）。"""
        a = self.agent
        mgr = a._session_mgr
        if mgr is None:
            return None
        from ..session import context as _ctx
        branch = mgr.get_branch()
        fake = _tree.Entry(type=_tree.COMPACTION, id="__pending_compaction__",
                           parentId=(branch[-1].id if branch else None),
                           sessionId=a._tree_session_id, timestamp=_tree.now_iso(),
                           data={"summary": summary, "firstKeptEntryId": first_kept})
        rich, _ = _ctx.fold(branch + [fake])
        return len(_ctx.convert_to_llm(rich))

    # ── state ↔ tree 同步（§6/§7）────────────────────────────────────────────
    def hydrate_state(self) -> AgentState:
        """从 canonical 树 build_context() 重建 AgentState（§6：可丢弃投影,绝非 durable truth）。

        分支折叠出的 scalar（provider/model/thinking/active_tools 末态）优先,保证 resume 忠实。
        缺写者租约 → fail-loud（树是唯一权威,无 flat 兜底）。"""
        a = self.agent
        if a._session_mgr is None:
            raise _tree.SessionTreeError("no writer lease: cannot hydrate AgentState without canonical tree")
        built = a._session_mgr.build_context()
        return AgentState.hydrate(
            built,
            provider=("openai" if a.use_openai else "anthropic"),
            model=a.model,
            system_prompt=a._system_prompt,
            thinking_level=a._thinking_mode,
            supports_images=True,
            total_input_tokens=a.total_input_tokens,
            total_output_tokens=a.total_output_tokens,
            last_input_token_count=a.last_input_token_count,
            current_turns=a.current_turns,
        )

    def record_event(self, event) -> bool:
        """把一条 AgentEvent 落成 canonical session entry（§7：唯一持久化通道）。

        返回是否真正写入（custom_message 写失败 → False,调用方据此决定 dedup/兜底）。
        消息族（user/assistant/toolResult）的 event.message 已是**中立 Message**——直接 append_message
        （不经 capture；capture 是 provider→中立 的逆向,中立再 capture 会丢 toolCall 块）。required=True
        写失败 fail-loud（删 flat 后必须,§7.6）。遥测族走 _tree_event（注解型,不推进 leaf）；context_injected
        走 _tree_custom_message。AssistantDelta/ToolCallRequested/ToolResultObserved/ErrorRaised
        无树等价物（仅 UI / 无持久化）。
        """
        a = self.agent
        k = getattr(event, "kind", None)
        if k == "user_message_accepted":
            # docs/16 #0：优先用已 capture 的中立 message（保留 block content）；缺则按 text 重建。
            neutral = getattr(event, "message", None) or _tree.user_message(event.text)
            self._append_neutral(neutral, required=True)
            return True
        if k == "assistant_message_completed":
            self._append_neutral(event.message, required=True)
            return True
        if k == "tool_result_completed":
            self._append_neutral(event.message, required=True)
            return True
        if k == "context_injected":
            return a._tree_custom_message(event.custom_type, event.content)
        if k == "llm_request_prepared":
            a._tree_event(_tree.LLM_REQUEST, model=event.model,
                          messageCount=event.message_count, messagesChars=event.messages_chars)
            return True
        if k == "tool_call_authorized":
            a._tree_event(_tree.PERMISSION_DECISION, tool=event.tool,
                          action=event.action, message=event.message)
            return True
        if k == "tool_blocked":
            a._tree_event(_tree.TOOL_BLOCKED, tool=event.tool, reason=event.reason,
                          agentType=a.agent_type, artifactId=a.artifact_id)
            return True
        if k == "budget_exceeded":
            a._tree_event(_tree.BUDGET_EXCEEDED, reason=event.reason)
            return True
        if k == "compaction_requested":
            # docs/16 #3a：compaction entry 也归单写者。append 失败可观测（丢 entry = 树渲染不收缩）。
            if a._session_mgr is None:
                return False
            try:
                a._session_mgr.append_compaction(
                    summary=event.summary or "", tokens_before=event.tokens_before,
                    first_kept_entry_id=event.first_kept_entry_id, kind=event.compaction_kind,
                    message_count_before=event.message_count_before,
                    message_count_after=event.message_count_after)
                return True
            except Exception as e:
                a._sink.info(f"[tree] compaction entry append failed: {e}")
                return False
        if k in ("turn_completed", "turn_aborted"):
            a._tree_event(_tree.TURN_END, inputTokens=event.input_tokens,
                          outputTokens=event.output_tokens, turns=event.turns,
                          finalStatus="cancelled" if k == "turn_aborted" else "completed")
            return True
        # assistant_delta / tool_call_requested / tool_result_observed / error_raised → 无树等价物
        return False

    def _append_neutral(self, neutral_msg: dict, *, required: bool) -> None:
        """把一条**中立 Message** append 进 canonical 树（绕过 capture）。required=True 写失败重抛（§7.6）。"""
        a = self.agent
        if a._session_mgr is None:
            if required:
                raise _tree.SessionTreeError("no writer lease for record_event (required write)")
            return
        try:
            a._session_mgr.append_message(neutral_msg)
        except Exception as e:
            try:
                a._sink.info(f"[tree] record_event failed: {e}")
            except Exception:
                pass
            if required:
                raise

    # ── in-file 导航（docs/14 P6）────────────────────────────────────────────
    def move_to(self, entry_id: "str | None", *, agent_id: str = "main") -> list:
        """把 active leaf 移到 canonical 树的 entry_id（in-file 导航 / checkout / fork-before）。
        entry_id=None → 复位 root（空上下文）。从该 leaf 重建上下文并装入 live agent。fail-closed：
        无写者租约 / entry 不存在 → ValueError。返回重建的消息列表。"""
        from ..session.render import ModelCtx, render
        a = self.agent
        mgr = a._session_mgr
        if mgr is None:
            raise ValueError("no active session writer lease; cannot navigate the tree")
        if entry_id is not None and entry_id not in {e.id for e in mgr.entries()}:
            raise ValueError(f"entry '{entry_id}' not found in session tree; session left unchanged")
        mgr.set_leaf(entry_id)
        built = mgr.build_context()
        provider = "openai" if a.use_openai else "anthropic"
        api = "openai-completions" if a.use_openai else "anthropic"
        sysp = a._system_prompt if a.use_openai else None
        msgs = render(built.messages, ModelCtx(provider=provider, api=api, model_id=a.model),
                      system_prompt=sysp)["messages"]
        a._load_messages(msgs)
        return msgs

    # ── turn-end 一致性（§7.6）────────────────────────────────────────────────
    def verify_turn_consistency(self) -> list[str]:
        """检查当前 branch 的 turn-end 不变量（§7.6）。返回问题字符串列表（空 = 一致）。

        删 flat fallback 后树是唯一权威——孤儿/断链/leaf 漂移会直接污染下一轮请求,必须可检测。
        检查：① build_context 可折叠（链可重建）；② 无 inverse-orphan toolResult（toolCallId 在
        branch 内无对应 toolCall）；③ leaf 指向 branch 末条 leaf-affecting entry；
        ④ compaction.firstKeptEntryId 若有则须在 branch 内可达。
        注：forward-orphan（toolCall 无 result）由 render positional 合成兜底,不算 turn-end 错误
        （但本检查仍报告,供诊断）。"""
        a = self.agent
        mgr = a._session_mgr
        issues: list[str] = []
        if mgr is None:
            return ["no writer lease: tree not available for consistency check"]
        try:
            branch = mgr.get_branch()
        except Exception as e:
            return [f"branch not reconstructable (broken/cyclic chain): {e}"]
        try:
            mgr.build_context()
        except Exception as e:
            issues.append(f"build_context failed (state not rebuildable): {e}")

        # 收集 branch 上的 toolCall id 与 toolResult toolCallId（中立 Message 形态）。
        call_ids: set[str] = set()
        result_ids: list[str] = []
        kept_first: str | None = None
        for e in branch:
            if e.type == _tree.COMPACTION:
                fk = e.data.get("firstKeptEntryId")
                kept_first = fk if fk else kept_first
            if e.type != _tree.MESSAGE:
                continue
            msg = e.data.get("message") or {}
            role = msg.get("role")
            if role == "assistant":
                for b in msg.get("content", []) or []:
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        call_ids.add(b.get("id"))
            elif role == "toolResult":
                result_ids.append(msg.get("toolCallId"))

        for rid in result_ids:
            if rid not in call_ids:
                issues.append(f"inverse-orphan toolResult: toolCallId {rid!r} has no matching toolCall in branch")

        # leaf 指向 branch 末条 leaf-affecting entry。
        leaf = mgr.get_leaf()
        expected = None
        for e in branch:
            r = _tree.leaf_id_after_entry(e)
            if r is not _tree._UNCHANGED:
                expected = r
        if leaf != expected:
            issues.append(f"leaf {leaf!r} != last leaf-affecting entry {expected!r}")

        # firstKeptEntryId 可达。
        if kept_first is not None and kept_first not in {e.id for e in branch}:
            issues.append(f"compaction firstKeptEntryId {kept_first!r} not reachable in branch")

        return issues
