"""AgentSession：state ↔ canonical 树的同步边界（docs/15 §7）。

docs/15 STEP D：从薄包装升级为「同时知道 SessionManager 与 AgentCore 的唯一对象」。
职责：
- hydrate_state()：从 canonical 树 build_context() 重建 AgentState（可丢弃投影,§6）。
- record_event(event)：把 AgentEvent 落成 canonical session entry（取代散落的 _tree_* 调用）。
- run_turn(prompt)：一个 turn 的编排外壳（当前委托 AgentCore=Agent.chat；事件 inversion 渐进上移）。
- compact()：压缩（委托 _compact_conversation，写 compaction entry + 两区 fold）。
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
        """压缩当前对话（写 compaction entry + 两区 fold）。instructions 预留（自定义摘要指令）。"""
        await self.agent._compact_conversation()

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
        走 _tree_custom_message。AssistantDelta/ToolCallRequested/ErrorRaised 无树等价物。
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
        if k in ("turn_completed", "turn_aborted"):
            a._tree_event(_tree.TURN_END, inputTokens=event.input_tokens,
                          outputTokens=event.output_tokens, turns=event.turns,
                          finalStatus="cancelled" if k == "turn_aborted" else "completed")
            return True
        # assistant_delta / tool_call_requested / error_raised / compaction_requested → 无直接树等价物
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
