"""AgentSession：state ↔ canonical 树的同步边界（docs/15 §7）。

docs/16 #3：从薄包装升级为 **turn shell**（= pi AgentHarness：事件订阅者 = 唯一树写入者 +
compaction owner + 注入/上下文构建归口）。职责：
- hydrate_state()：从 canonical 树 build_context() 重建 AgentState（可丢弃投影,§6）。
- record_event(event)：把 AgentEvent 落成 canonical session entry（emit 单出口的树腿）。
- run_turn(prompt)：一个 turn 的编排外壳。
- compact() / check_and_compact()：compaction owner（docs/16 #3a）——summarizer 输入吃树渲染,
  entry 经 CompactionRequested→record_event 单写（两区 fold 的唯一 shrink 通道）。
- build_request_messages()：每轮从树渲染请求（docs/13 S2,无 flat fallback）。
- inject_*()：四个 turn-boundary 注入器（session-context / skill 清单 / skill body /
  finished tasks）——树是唯一注入通道,dedup 只在写成功后推进。
- clear_history() / auto_save()：/clear 的 leaf 复位与 v2 derived-cache 落盘。
- move_to()：in-file 树导航（移 leaf）。
- verify_turn_consistency()：turn-end 一致性检查（§7.6）——删 flat fallback 后,树是唯一权威,
  孤儿/断链/leaf 漂移必须 fail-loud 而非静默污染下一轮。

边界：AgentSession 是**唯一**调 SessionManager.append_* 的高层对象（经 agent._session_mgr）；
AgentCore 只发事件、不写 session。子 agent 经各自的 agent_session 写自己的 child 树。
"""

from __future__ import annotations

import os

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
            return self._tree_custom_message(event.custom_type, event.content)
        if k == "llm_request_prepared":
            self._tree_event(_tree.LLM_REQUEST, model=event.model,
                          messageCount=event.message_count, messagesChars=event.messages_chars)
            return True
        if k == "tool_call_authorized":
            self._tree_event(_tree.PERMISSION_DECISION, tool=event.tool,
                          action=event.action, message=event.message)
            return True
        if k == "tool_blocked":
            self._tree_event(_tree.TOOL_BLOCKED, tool=event.tool, reason=event.reason,
                          agentType=a.agent_type, artifactId=a.artifact_id)
            return True
        if k == "budget_exceeded":
            self._tree_event(_tree.BUDGET_EXCEEDED, reason=event.reason)
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
            self._tree_event(_tree.TURN_END, inputTokens=event.input_tokens,
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

    # ── 树写入 legs（record_event 内部；docs/16 #3b 自 engine 迁入）─────────────
    def _tree_event(self, entry_type: str, **data) -> None:
        """把一条派生遥测写成**注解型**树 entry（不在 FOLD_TYPES、不推进 leaf、对 LLM 不可见），
        供 trajectory 从树派生。唯一调用方 = record_event（emit 单出口的树腿）。失败**可观测**
        （sink.info，不静默）但不破坏 live turn——遥测是注解、非 message family。
        **防御性剔除 reward/eval_result**——派生标签绝不进事实源（docs/10 三层边界）。"""
        a = self.agent
        data.pop("reward", None)
        data.pop("eval_result", None)
        try:
            if a._session_mgr is not None:
                a._session_mgr.append(entry_type, data)
        except Exception as e:
            a._sink.info(f"[tree] telemetry append failed ({entry_type}): {e}")

    def _tree_custom_message(self, custom_type: str, content, *, parent_id: "str | None" = None) -> bool:
        """把一次注入作为 custom_message entry 写进 canonical 树（主 agent=自身树，子 agent=child 树；
        按 _session_mgr 而非 is_sub_agent gate）。唯一调用方 = record_event（ContextInjected 的树腿）。
        返回是否真正写入——emit 调用方据此推进 dedup（docs/14 P3 review #7）；写失败**可观测**。"""
        a = self.agent
        if a._session_mgr is None:
            return False
        try:
            a._session_mgr.append(_tree.CUSTOM_MESSAGE,
                                  {"customType": custom_type, "content": content, "display": False},
                                  parent_id=parent_id)
            return True
        except Exception as e:
            a._sink.info(f"[tree] custom_message append failed ({custom_type}): {e}")
            return False

    # ── 请求构建（docs/13 S2；#3b 自 engine 迁入）────────────────────────────────
    def build_request_messages(self) -> list:
        """从 canonical 树渲染本轮请求（`render(build_context())`）。

        树是会话**唯一**事实源（含 message-end 写入的消息 + 注入的 custom_message）；render 据当前
        provider 整形 + 合并相邻 user。**无 flat fallback**：缺 writer lease 即 fatal（lease prologue
        已在 turn 开始保证 _session_mgr 存在）。Anthropic system 走 out-of-band，OpenAI system 经
        render 注入 index 0。"""
        a = self.agent
        if a._session_mgr is None:
            raise _tree.SessionTreeError("no writer lease: cannot build request messages without canonical tree")
        from ..session.render import ModelCtx, render
        provider = "openai" if a.use_openai else "anthropic"
        api = "openai-completions" if a.use_openai else "anthropic"
        sysp = a._system_prompt if a.use_openai else None
        built = a._session_mgr.build_context()
        return render(built.messages, ModelCtx(provider=provider, api=api, model_id=a.model),
                      system_prompt=sysp)["messages"]

    # ── turn-boundary 注入器（docs/16 #3b 自 engine 迁入；树是唯一注入通道）───────
    _SESSION_CONTEXT_KINDS = frozenset({"project_instructions", "memory_static"})

    async def inject_session_context(self) -> None:
        """把项目指令 + memory 静态段作为 session-context custom_message 注入 canonical 树（§8.3）。

        取代旧的「烤进 system prompt」：稳定 system 前缀利于 cache,内容作 messages/custom_message。
        仅主 agent（子 agent 各有自己的 system prompt,不应注入项目指令）。

        幂等 + resume 安全 + compaction 存活,统一由「**经 fold 实际渲染**的 session-context 包」判定
        （_session_context_present_kinds 直接用 context.fold,与发给模型的上下文一致）。
        **per-customType** 去重:只注入当前缺失的 kind,不被「另一 kind 在场」抑制。"""
        a = self.agent
        if a.is_sub_agent:                                # main agent 的 lease 由 turn prologue 先行保证
            return
        present = self._session_context_present_kinds()
        if present >= self._SESSION_CONTEXT_KINDS:        # 全部 kind 已在渲染上下文 → 无需注入
            return
        from ..context import BudgetPolicy, ContextRequest, ContextRuntime
        req = ContextRequest(
            cwd=os.getcwd(), is_sub_agent=False,
            include_project_instructions=True, include_memory=True,
            include_env=False, include_git=False, include_skills=False,
            include_agents=False, include_deferred_tools=False,
        )
        try:
            plan = await ContextRuntime(budget=BudgetPolicy.for_window(a.effective_window)).collect(req)
        except Exception as e:
            a._sink.info(f"[context] session-context collect failed: {e}")
            return
        for pack in plan.packs:
            if pack.kind not in present:                  # per-customType：只注入缺失的 kind
                a.emit(_events.ContextInjected(custom_type=pack.kind, content=pack.content))

    def _session_context_present_kinds(self) -> set[str]:
        """当前 branch **经 context.fold 实际渲染**的 session-context customType 集合。

        用 fold（而非手算 post-compaction 区间）确保与真正发给模型的上下文一致：fold 的 rich 输出保留
        custom_message 的 customType（convert_to_llm 才丢弃）,故能精确判定「模型是否看得到该包」。"""
        mgr = self.agent._session_mgr                     # lease 先行保证非 None（docs/16 C-1）
        try:
            from ..session import context as _ctx
            rich, _ = _ctx.fold(mgr.get_branch())
        except Exception:
            return set(self._SESSION_CONTEXT_KINDS)       # 树不可重建 → 保守视为全在(不重复注入)
        return {m.get("customType") for m in rich
                if m.get("role") == "custom" and m.get("customType") in self._SESSION_CONTEXT_KINDS}

    def _skill_listing_budget(self) -> int:
        return max(2000, int(self.agent.effective_window * 0.04))

    def inject_skill_listing(self) -> None:
        """skill 清单 → ContextInjected 事件 → custom_message entry。
        dedup（_sent_skill_names）**只在树写成功后**推进——失败则下一轮重试，不静默丢清单。"""
        a = self.agent
        if a.is_sub_agent:
            return
        from ..skills.listing import skill_listing_delta
        text, new_names = skill_listing_delta(
            a._sent_skill_names, a._activated_path_skills, self._skill_listing_budget()
        )
        if text and a.emit(_events.ContextInjected(custom_type="skill_listing", content=text)):
            a._sent_skill_names.update(new_names)

    def inject_pending_skill_bodies(self) -> None:
        """skill body → ContextInjected 事件 → custom_message entry。
        树写失败的 body 留在队列、下一轮重试（不静默丢指令）。"""
        a = self.agent
        from ..skills.listing import render_skill_body_message
        remaining: list = []
        for name, body in a._pending_skill_bodies:
            msg = render_skill_body_message(name, body)
            if not a.emit(_events.ContextInjected(custom_type="skill_body", content=msg.get("content", ""))):
                remaining.append((name, body))
        a._pending_skill_bodies = remaining

    def inject_finished_tasks(self) -> None:
        """turn boundary 注入：终态且未注入的后台任务渲染成 <system-reminder>，写 custom_message entry
        挂在 **live leaf**（必须在当前 branch 上，否则模型看不到完成提醒——这优先于 docs/14 §6b 的
        "pin 到 spawn 分支"）。spawn 血缘记在 task.spawn_entry_id（state.json 持久）供审计。
        树写失败 → 不标 injected、下一轮重试，不静默丢提醒。"""
        a = self.agent
        if a.is_sub_agent:
            return   # 子 agent 与父共享 TaskManager；finished-task 回注是**父**（user-facing loop）的职责，
                     # 否则子会"偷走"并标 injected 父/兄弟的后台完成提醒，使父永不浮现（review high）。
        from ..tasks.inject import collect_pending_injections, render_task_reminder
        pending = collect_pending_injections(a.task_manager)
        if not pending:
            return
        text = "\n\n".join(render_task_reminder(t) for t in pending)
        if not a.emit(_events.ContextInjected(custom_type="finished_tasks", content=text)):
            return
        for t in pending:
            a.task_manager.update_task(t.id, injected=True)

    # ── /clear 与 derived-cache 落盘（docs/16 #3b 自 engine 迁入）────────────────
    def clear_history(self) -> None:
        """docs/14 SessionLease：/clear = 把 active leaf 复位到 root（in-file），而非清空对话事实。
        历史保留在 canonical 树里（可经 /tree 回看 / 在旧分支继续）；后续 turn 从 root 起一条新分支。
        复位本对话的 working set + 计数。flat else 分支已删（docs/16 C-2 随 #3）：缺 lease 即自取
        （headless /clear-before-first-turn 也走树路径）。"""
        a = self.agent
        a._ensure_session_lease()
        a._session_mgr.set_leaf(None)        # 回到 root：get_branch(None)==[] → 空上下文
        from ..session.render import ModelCtx, render
        provider = "openai" if a.use_openai else "anthropic"
        api = "openai-completions" if a.use_openai else "anthropic"
        sysp = a._system_prompt if a.use_openai else None
        built = a._session_mgr.build_context()
        a._load_messages(render(built.messages,
                                ModelCtx(provider=provider, api=api, model_id=a.model),
                                system_prompt=sysp)["messages"])
        a.total_input_tokens = 0
        a.total_output_tokens = 0
        a.last_input_token_count = 0
        a._sent_skill_names = set()
        a._pending_skill_bodies = []
        a._activated_path_skills = set()
        a._active_hooks = []
        from ..skills.discovery import reset_skill_cache
        reset_skill_cache()
        a._sink.info("Conversation cleared (leaf reset to root; history kept — /tree to revisit).")

    def auto_save(self) -> None:
        """v2 state.json（TaskManager/subagent 派生 cache）按需落盘——canonical 树是 resume 权威。
        含 list_tasks：仅有后台 shell 任务、无 subagent 的 session 也要落 state（docs/14 P2 review）。"""
        a = self.agent
        from ..session import v2 as _session_v2
        if (_session_v2.is_v2_session(a.session_id) or a.task_manager.list_subagents()
                or a.task_manager.list_tasks()):
            a._persist_state()

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
