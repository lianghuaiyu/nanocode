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
from typing import NamedTuple

from . import tree as _tree
from ..agent import events as _events
from ..agent.state import AgentState


class CutPoint(NamedTuple):
    """compaction kept-suffix 起点 + 可观测元数据（docs/18 Phase 2/4）。

    first_kept_id：firstKeptEntryId（None = 无 kept suffix，前区全由 summary 顶替）；
    cut_entry_type：cut 头 entry 类型（message|custom_message|branch_summary|compaction|None）；
    is_split_turn：是否在单个超长 turn 内部切（cut 在非 user 边界）；
    kept_token_estimate：kept suffix 的 token 估计。"""

    first_kept_id: "str | None"
    cut_entry_type: "str | None"
    is_split_turn: bool
    kept_token_estimate: int


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

    # ── turn 编排（docs/16 #3c：turn shell = pi executeTurn）──────────────────
    async def run_turn(self, prompt: str) -> None:
        """一个 turn 的完整外壳：MCP lazy init → lease prologue → session-context 注入 →
        user 消息 emit → compaction 门 → AgentCore 纯 loop（state+cfg+emit）→ turn_end → auto_save。

        取消语义（不可回归契约）：CancelledError 吞成 _aborted=True 并正常返回；
        TurnResult 的 cancelled 状态由调用方 await 之后读 agent._aborted 映射。"""
        import asyncio
        a = self.agent
        # MCP lazy init（主 agent 首 turn；工具表扩充须在 cfg 快照之前）
        if not a._mcp_initialized and not a.is_sub_agent:
            a._mcp_initialized = True
            try:
                await a._mcp_manager.load_and_connect(
                    notify=lambda text, level="info": a.emit(_events.NoticeRaised(text=text, level=level)))
                mcp_defs = a._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    # docs/24 Phase 4b：MCP 工具并入 agent 的 per-agent overlay registry（不写全局
                    # REGISTRY）。每个注册为 Tool(schema=mcp 开放 schema **不 _closed**——保 MCP arg
                    # 校验不收紧；run=None；source=MCP；trust=UNTRUSTED；needs=∅；name=mcp__server__tool)。
                    # 执行经 engine._run_real_tool 的 source 判定路由回 mcp_manager.call_tool。
                    from ..tools.spec import Tool
                    from ..tools.types import ToolSource, Trust
                    for d in mcp_defs:
                        a._registry.register(Tool(
                            schema=d, run=None, source=ToolSource.MCP, trust=Trust.UNTRUSTED,
                            needs=frozenset()))
                    a.tools = a._registry.schemas()
            except Exception as e:
                a.emit(_events.NoticeRaised(text=f"[mcp] Init failed: {e}"))

        a._aborted = False
        a._pending_context_break = False        # turn-scoped：上一 turn 的遗留信号绝不跨 turn
        a._ensure_session_lease()               # lease prologue（runtime 已注入则 no-op）
        from ..context import ContextLedger
        a._context_ledger = ContextLedger()     # per-turn 全量记账（/context 可见性，docs/16 #6）
        a._turn_context_plan = None             # clear previous turn's volatile tail before any render/compact
        await self.inject_session_context()     # 项目指令/memory 作 session-context 包
        a.emit(_events.UserMessageAccepted(text=prompt))   # S1：先回显/落树，耗时 repo-map 不遮住提交反馈
        a._turn_context_plan = await self._collect_turn_volatile(prompt)   # date/git/repo-map 等 per-turn volatile
        await self.check_and_compact()
        memory_prefetch = self.start_memory_prefetch(prompt)

        state = self.hydrate_state()
        cfg = self._loop_config(memory_prefetch)
        a._current_task = asyncio.current_task()
        try:
            await a._core.run_turn(state, cfg, a.emit, stream_fn=a._provider.stream)
        except asyncio.CancelledError:
            a._aborted = True
        finally:
            a._current_task = None
        # turn-end 累计遥测落树（trajectory 的 total_turns + 终态 step 从此派生；record_event 单写）。
        if a._aborted:
            a.emit(_events.TurnAborted(input_tokens=a.total_input_tokens,
                                       output_tokens=a.total_output_tokens, turns=a.current_turns))
        else:
            # docs/17 Phase 2：TurnCompleted 携 cost_usd，订阅端（含 RPC）据此渲染成本，无需自带定价。
            a.emit(_events.TurnCompleted(input_tokens=a.total_input_tokens,
                                         output_tokens=a.total_output_tokens, turns=a.current_turns,
                                         cost_usd=a._get_current_cost_usd()))
        if not a.is_sub_agent:
            self.auto_save()

    def _loop_config(self, memory_prefetch=None):
        """绑定本 turn 的 AgentLoopConfig（docs/16 #3c）：scalars 快照 + 宿主能力闭包。
        execute_tool 必须是 allowlist fail-closed 咽喉点入口（_execute_tool_call→router.dispatch）。"""
        from ..agent.loop import AgentLoopConfig
        from ..tools import get_active_tool_definitions
        a = self.agent

        def note_api_call() -> None:
            import time
            a.last_api_call_time = time.time()

        def add_usage(input_tokens: int, output_tokens: int) -> None:
            a.total_input_tokens += input_tokens
            a.total_output_tokens += output_tokens
            a.last_input_token_count = input_tokens

        def bump_turn() -> None:
            a.current_turns += 1

        def consume_context_break() -> bool:
            if a._pending_context_break:
                a._pending_context_break = False
                return True
            return False

        def inject_turn_context() -> None:
            if a.is_sub_agent:
                from ..subagents.steer import drain_pending_steers
                drain_pending_steers(a, delivery="steer")
            self.inject_finished_tasks()
            self.inject_skill_listing()

        def inject_follow_up() -> bool:
            if not a.is_sub_agent:
                return False
            from ..subagents.steer import drain_pending_steers
            return drain_pending_steers(a, delivery="follow_up") > 0

        return AgentLoopConfig(
            model=a.model, thinking_mode=a._thinking_mode,
            resolve_tools=lambda: get_active_tool_definitions(a.tools, registry=a._registry),
            is_sub_agent=a.is_sub_agent,
            rebuild_snapshot=self.project_request,
            to_completion=a._provider.complete,
            record_provider_messages=self.record_provider_messages,
            tool_result_messages=a._provider.tool_result_messages,
            execute_tool=a._execute_tool_call,
            authorize=a._authorize_dispatch,
            persist_large_result=a._persist_large_result,
            check_budget=a._check_budget,
            bump_turn=bump_turn, note_api_call=note_api_call, add_usage=add_usage,
            token_totals=lambda: (a.total_input_tokens, a.total_output_tokens),
            is_aborted=lambda: a._aborted,
            compact=self._compact_on_overflow,
            consume_context_break=consume_context_break,
            inject_turn_context=inject_turn_context,
            inject_follow_up=inject_follow_up,
            inject_skill_bodies=self.inject_pending_skill_bodies,
            poll_memory=lambda: self.consume_memory_prefetch(memory_prefetch),
        )

    def record_provider_messages(self, provider_msg: dict, *, stop_reason: "str | None" = None,
                                 usage: "dict | None" = None, latency_ms: "int | None" = None) -> None:
        """message family 唯一树写入口（docs/16 #1）：capture-at-emit（#0 工厂）→ emit → record_event
        （required=True，写失败 fail-loud——树是唯一权威，缺一条 = 下一轮上下文错误）。"""
        a = self.agent
        for ev in _events.events_from_provider_message(
                provider_msg, provider=a._provider.name,
                model=a.model, stop_reason=stop_reason, usage=usage, latency_ms=latency_ms):
            a.emit(ev)

    def project_request(self):
        """每请求的 ProviderProjection：messages = 树渲染 + **volatile tail**（docs/16 #6），system 按
        provider 分流（anthropic out-of-band、openai 已在 messages[0]）。读 **live** _system_prompt——
        plan-mode 的 turn 内 system 切换经下一次重渲染实时生效。

        volatile tail：persist=none 的 per-turn packs（date/git…）以 render-time 装饰追加在请求**尾部**
        ——不入树（树存干净原文）、置尾不破坏 prompt-cache 稳定前缀；每 turn 收集一次
        （_collect_turn_volatile），turn 内各迭代复用（git subprocess per-turn 缓存）。"""
        from ..agent.state import ProviderProjection
        a = self.agent
        return ProviderProjection(provider=a._provider.name,
                                  messages=self.build_request_messages(extra_neutral=self._volatile_tail()),
                                  system=(None if a._provider.places_system_in_messages else a._system_prompt))

    def _volatile_tail(self) -> "list[dict] | None":
        """本 turn 的 volatile packs → 一条中立 user 消息（render 会与相邻 user 合并，保持 provider
        交替约束）。无 plan / 无 persist=none pack → None。"""
        plan = getattr(self.agent, "_turn_context_plan", None)
        if plan is None:
            return None
        vols = [p for p in plan.packs if p.persist_policy == "none"]
        if not vols:
            return None
        body = "\n\n".join(p.as_text() for p in vols)
        return [_tree.user_message(
            "<system-reminder>\nPer-turn context (refreshed every turn; supersedes any earlier "
            "snapshot in this conversation):\n" + body + "\n</system-reminder>")]

    async def _collect_turn_volatile(self, prompt: str = ""):
        """每 turn 一次的 volatile 上下文收集（docs/16 #6：date/git 移出 system prompt 的承接路径；
        repo map 同管道注入——aider-style，prompt 供提及提取，files_read/modified 用宿主观测事实）。

        collect 纯（产 packs 不写树）；ledger 并入本 turn 全量账本。仅主 agent（子 agent 的
        system prompt 来自 manifest，从未含 date/git——保持不变）。失败可观测、不破坏 turn。"""
        a = self.agent
        if a.is_sub_agent:
            return None
        from ..context import BudgetPolicy, ContextRequest, ContextRuntime
        from ..context.providers import EnvProvider, GitSnapshotProvider, RepoMapProvider
        from ..context.model_policy import model_uses_repo_map
        from ..tools.permissions import load_context_config
        from ..runtime import _push_cwd
        services = getattr(a, "_runtime_services", None)
        cwd = services.cwd if services is not None else os.getcwd()
        budget = BudgetPolicy.for_window(a.effective_window)
        with _push_cwd(cwd):
            cfg = load_context_config()
        configured_map_tokens = cfg["map_tokens"]
        map_tokens = (1024 if configured_map_tokens is None and model_uses_repo_map(a.model)
                      else (configured_map_tokens or 0))
        req = ContextRequest(cwd=cwd, is_sub_agent=False,
                             include_env=True, include_git=True,
                             include_repo_map=map_tokens > 0,
                             user_prompt=prompt,
                             files_read=sorted(a._files_read),
                             files_modified=sorted(a._files_modified),
                             map_tokens=map_tokens,
                             context_window_tokens=a.effective_window,
                             map_refresh=cfg["map_refresh"],
                             map_multiplier_no_files=cfg["map_multiplier_no_files"],
                             include_project_instructions=False, include_memory=False,
                             include_skills=False, include_agents=False,
                             include_deferred_tools=False)
        try:
            git_source = services.context_sources.git if services is not None else None
            plan = await ContextRuntime(providers=[EnvProvider(), GitSnapshotProvider(git_source),
                                                   RepoMapProvider()],
                                        budget=budget).collect(req)
        except Exception as e:
            a.emit(_events.NoticeRaised(text=f"[context] per-turn volatile collect failed: {e}"))
            return None
        led = getattr(a, "_context_ledger", None)
        if led is not None:
            led.entries.extend(plan.ledger.entries)
        return plan

    # ── memory prefetch（docs/16 #3c：宿主侧 helper，loop 经 cfg 调用）──────────
    def start_memory_prefetch(self, user_message: str):
        """主 agent 每 turn 启动 no-LLM 快速记忆预取（子 agent / 无 service → None）。"""
        a = self.agent
        if a.is_sub_agent or a._memory_service is None:
            return None
        return a._memory_service.start_prefetch(
            user_message, already_surfaced=a._already_surfaced_memories,
            session_memory_bytes=a._session_memory_bytes)

    def consume_memory_prefetch(self, prefetch) -> None:
        """settle 后注入一次：树是唯一注入通道（写失败 → 不推进 dedup，下一轮 prefetch 重新浮现）；
        召回/注入失败可观测（sink.info），不破坏 live turn（docs/16 #2 清 silent pass）。"""
        a = self.agent
        if not (prefetch and prefetch.settled and not prefetch.consumed):
            return
        prefetch.consumed = True
        try:
            memories = prefetch.task.result()
            if memories:
                from ..context.providers import MemoryRecallProvider
                provider = MemoryRecallProvider(a, memories)
                pack = provider.collect()
                if pack is not None:
                    ok = a.emit(_events.ContextInjected(custom_type=pack.kind, content=pack.content))
                    if ok:
                        provider.commit()      # 写成功才推进 dedup（_already_surfaced + 字节预算）
                    self._ledger_note(pack, ok)
        except Exception as e:
            a.emit(_events.NoticeRaised(text=f"[memory] recall injection failed: {e}"))

    async def _compact_on_overflow(self) -> None:
        """core per-turn provider-overflow 恢复入口（绑为 cfg.compact）：标记 trigger=overflow_retry 后压缩。
        与用户 /compact（manual）、turn-start auto 在 details.trigger 上可区分。"""
        self.agent._compaction_trigger = "overflow_retry"
        await self.compact()

    async def compact(self, instructions: str | None = None) -> None:
        """压缩当前对话（docs/16 #3a/#10：compaction owner = turn shell）。

        keepRecentTokens（#10）：kept-suffix 起点 = 预算内最近的 **user MESSAGE** entry
        （_compaction_cut_point）；summarizer 只吃 **prefix 投影**（cut 之前的 fold→render）——
        cut-point 与 summarizer kept-suffix 同一来源，summary 与 fold 保留区绝不双计。
        产出经 `CompactionRequested→record_event` 写 COMPACTION entry（两区 fold 的唯一
        shrink 通道）。instructions 来自 `/compact [prompt]` 的自定义摘要指令。"""
        a = self.agent
        mgr = a._session_mgr
        if mgr is None:
            a.emit(_events.NoticeRaised(text="Nothing to compact (no active session tree)."))
            return
        # trigger 判定（docs/18 Phase 4）：instructions 给出即必为 manual；否则读 check_and_compact 设的
        # 瞬态标记（auto），缺省 manual（含 overflow 恢复路径——它走同一无参 compact）。读后即复位。
        trigger = "manual" if instructions else getattr(a, "_compaction_trigger", "manual")
        a._compaction_trigger = "manual"
        a._compacting = True
        try:
            tokens_before = a.last_input_token_count
            before_count = len(mgr.build_context().messages)
            branch = mgr.get_branch()
            cut = self._compaction_cut(branch)
            first_kept = cut.first_kept_id
            cut_idx = (next((i for i, e in enumerate(branch) if e.id == first_kept), len(branch))
                       if first_kept is not None else len(branch))
            prefix_branch = branch[:cut_idx]
            raw_summary, retry_count = await self._summarize_prefix_with_retry(prefix_branch, instructions)
            from ..agent.summary_prompts import format_compact_summary
            summary = format_compact_summary(raw_summary) if raw_summary else (raw_summary or "")
            # review MED：summarizer 真的产出了内容、但 format 后为空（模型只吐 <analysis>/空 <summary>）=
            # 退化输出，绝非"无可压缩"的 no-op——若放过，check_and_compact 会清零熔断、context 不缩，下一轮
            # 又发一次真实 summarizer LLM 调用，每 turn 无限浪费。视为失败 → fail-loud（auto 计入熔断）。
            if raw_summary and not summary:
                raise _tree.SessionTreeError(
                    "summarizer returned no usable summary (analysis-only/empty); compaction failed")
            if summary:
                from ..context.packs import estimate_tokens
                read_files, modified_files = self._compaction_file_tracking(branch)
                message_count_after = self._predicted_post_compaction_count(summary, first_kept)
                reason = ("manual" if instructions else
                          ("prompt_too_long" if (retry_count or trigger == "overflow_retry")
                           else "context_window"))
                details = {
                    "trigger": trigger,
                    "reason": reason,
                    "cutEntryType": cut.cut_entry_type,
                    "isSplitTurn": cut.is_split_turn,
                    "retryCount": retry_count,
                    "tokensBefore": tokens_before,
                    "estimatedPostCompactTokens": cut.kept_token_estimate + estimate_tokens(summary),
                    "messageCountBefore": before_count,
                    "messageCountAfter": message_count_after,
                    "readFiles": read_files,
                    "modifiedFiles": modified_files,
                }
                written = self.record_event(_events.CompactionRequested(
                    reason="context_window", tokens_before=tokens_before, summary=summary,
                    first_kept_entry_id=first_kept,
                    message_count_before=before_count,
                    message_count_after=message_count_after,
                    details=details,
                    compaction_kind=("manual" if trigger == "manual" else "auto"),
                ))
                # review HIGH: compaction entry append 失败 = 树**没收缩**——绝不能当成功（否则
                # check_and_compact 会把失败的 auto 压缩误判为成功、清零失败计数）。fail-loud → auto
                # 路径据此 +1 计入熔断、手动 /compact 由命令层显示。
                if not written:
                    raise _tree.SessionTreeError(
                        "compaction entry append failed; conversation did not shrink")
                await self._restore_context_after_compaction()
                a.emit(_events.NoticeRaised(text="Conversation compacted."))
                a._sent_skill_names = set()  # 清单消息被压缩丢弃 → 下一轮重新播报
        finally:
            a._compacting = False
            a._summarizer_partial = False   # review：retry 标记不跨 compaction 残留（防陈旧 partial prompt）

    async def _restore_context_after_compaction(self) -> None:
        """compaction 后立即恢复 session-level 上下文（docs/18 Phase 5）。

        两区 fold 会把 cut 之前注入的 session-context custom_message（project_instructions /
        memory_static）折叠进 summary——若不恢复，本 turn 余下迭代将丢失它们（要等下一 turn 的
        inject_session_context 才回来）。这里复用 inject_session_context 的 **fold 可见 customType 去重
        + survival matrix**：它注入的 packs 均 lifecycle='session'（survives_compaction=True），只把被
        折掉的 survivor 重新注入为 post-compaction leaf 上的新 custom_message（绝不原地改写历史 entry）。

        刻意**不**恢复：repo map / env·date·git volatile（lifecycle='turn'）、memory recall（'turn'）、
        skill body（'one_shot'）、path-triggered one-shot reminder——它们 survives_compaction=False，
        本就不应跨 compaction 存活。plan-mode 提示在 live system prompt（render 每请求重读、不入树），
        compaction 不影响、无需恢复。子 agent 无 session-context 注入（system prompt 来自 manifest）。"""
        await self.inject_session_context()

    def _compaction_file_tracking(self, branch) -> "tuple[list[str], list[str]]":
        """累计 read/modified 文件（docs/18 Phase 4）：宿主**真实观测**的 _files_read/_files_modified
        （engine._on_file_touched，绝非 repo map 文本）∪ 分支上 assistant 真实 toolCall 的 read/edit/write
        file_path 参数（review：resume/rebind 后 live 集合被清空，须从树里的 toolCall 找回压缩前已读）∪
        既有 compaction/branch_summary entry 的 details.readFiles/modifiedFiles（跨多代压缩累计）。
        返回 (sorted readFiles, sorted modifiedFiles)。"""
        from . import branch_summary as _bs
        a = self.agent
        read = set(a._files_read)
        modified = set(a._files_modified)
        for e in branch:
            if e.type in (_tree.COMPACTION, _tree.BRANCH_SUMMARY):
                d = e.data.get("details") or {}
                read.update(d.get("readFiles") or [])
                modified.update(d.get("modifiedFiles") or [])
            elif e.type == _tree.MESSAGE:
                msg = e.data.get("message") or {}
                if msg.get("role") == "assistant":
                    for b in (msg.get("content") or []):
                        if isinstance(b, dict) and b.get("type") == "toolCall":
                            _bs._track_tool_call(b.get("name"), b.get("arguments"), read, modified)
        return sorted(read), sorted(modified)

    async def _summarize_prefix_with_retry(self, prefix_branch, instructions: "str | None"):
        """summarizer 调用 + prompt-too-long 降级重试（docs/18 Phase 3）。返回 (summary, retry_count)。

        summarizer **自身**请求溢出（is_context_overflow_error）时，最多重试 3 次，每次丢弃**最旧一个
        完整 API round**（_drop_oldest_round，按 user 边界切，绝不腰斩 tool_use/tool_result 对），并经
        _project_branch_prefix 重新渲染（fold→convert_to_llm→render，保证 provider 合法）——绝非字符串
        截断。无法再收缩 / 重试耗尽 / 非溢出错误 → 上抛（auto 路径由 check_and_compact 计入失败计数、
        手动 /compact 由命令层显示）。与 core 的 per-turn overflow 恢复（重压**实时**对话）是不同层。"""
        from ..agent.providers import is_context_overflow_error
        a = self.agent
        retry_count = 0
        while True:
            # review：retry（丢了最旧 round）时告知 summarizer 这是**部分视图**——经 host 瞬态标记驱动
            # core._compact_* 选 partial_compact_prompt（不改 _compact_* 外部签名，monkeypatch stub 不受影响）。
            a._summarizer_partial = retry_count > 0
            prefix_messages = self._project_branch_prefix(prefix_branch)
            try:
                return (await a._compact(prefix_messages, instructions), retry_count)
            except Exception as e:
                dropped = self._drop_oldest_round(prefix_branch)
                if (retry_count >= 3 or not is_context_overflow_error(e)
                        or not dropped or len(dropped) == len(prefix_branch)):
                    raise
                prefix_branch = dropped
                retry_count += 1
                a.emit(_events.NoticeRaised(
                    text=f"[compaction] summary request too long — dropping oldest round and "
                         f"retrying ({retry_count}/3)...", level="warn"))

    @staticmethod
    def _drop_oldest_round(entries):
        """丢弃最旧一个完整 API round：返回从**第 2 条 user MESSAGE** 起的子列表（按 user 边界切，
        保留 tool_use/tool_result 配对、绝不腰斩一个 round）。不足 2 个 user 边界 → []（无法再缩）。"""
        users = [i for i, e in enumerate(entries)
                 if e.type == _tree.MESSAGE and (e.data.get("message") or {}).get("role") == "user"]
        return entries[users[1]:] if len(users) >= 2 else []

    def _compaction_policy(self):
        """本会话的 CompactionPolicy（docs/18 Phase 1）：按 effective_window + 模型最大输出构造。
        summary_output_reserve = min(_get_max_output_tokens(model), 20k)；无元数据的模型由
        _get_max_output_tokens 返回保守默认（16384）。"""
        from ..agent.models import _get_max_output_tokens
        from .compaction_policy import CompactionPolicy
        a = self.agent
        return CompactionPolicy.for_model(a.effective_window, _get_max_output_tokens(a.model))

    async def check_and_compact(self) -> None:
        """auto-compact 阈值门 + 失败熔断（turn shell 职责，docs/18 Phase 1）。

        门控顺序（不变量）：① abort 门控**最先**（docs/16 #10：被取消的 turn 不值得一次 summarizer
        调用）；② reentrancy 守卫（压缩进行中不重入）；③ 失败熔断（连续 auto 失败达
        policy.max_consecutive_failures → 跳过本 session 后续 auto compact；手动 /compact 不经此门、
        不受限）；④ 阈值（CompactionPolicy.auto_threshold 取代旧的 0.85*window）。

        失败计数只在 auto 路径累计：summarizer 异常被吞掉（auto 压缩失败不该炸掉整个 turn——
        下一请求若真溢出由 core 的 overflow 恢复兜底）并 +1；成功（含"无可压缩内容"的 no-op 正常返回）
        清零。手动 /compact 直接调 compact()、绝不经过本门，故其异常照常上抛给 /compact 命令显示。"""
        a = self.agent
        if a._aborted:
            return
        if a._compacting:
            return
        policy = self._compaction_policy()
        if a._consecutive_compaction_failures >= policy.max_consecutive_failures:
            return
        if a.last_input_token_count > policy.auto_threshold:
            a.emit(_events.NoticeRaised(text="Context window filling up, compacting conversation..."))
            a._compaction_trigger = "auto"      # docs/18 Phase 4：标记本次为 auto（compact 读后复位）
            try:
                await self.compact()
            except Exception as e:
                a._consecutive_compaction_failures += 1
                a.emit(_events.NoticeRaised(
                    text=f"[compaction] auto-compaction failed "
                         f"({a._consecutive_compaction_failures}/{policy.max_consecutive_failures}): {e}",
                    level="warn"))
            else:
                a._consecutive_compaction_failures = 0

    def keep_recent_tokens(self) -> int:
        """compaction 保留的近期 suffix 预算（docs/18 Phase 1）：委托 CompactionPolicy
        （有效窗口 15%，下限 4k、上限 20k）。供 _compaction_cut_point 作 suffix 预算。"""
        return self._compaction_policy().keep_recent_tokens()

    def _compaction_cut_point(self, branch) -> "str | None":
        """kept-suffix 起点 id（兼容旧签名）：委托 _compaction_cut。fold 对 None 的语义是"无 kept
        suffix"（前区全由 summary 顶替）。"""
        return self._compaction_cut(branch).first_kept_id

    def _compaction_cut(self, branch) -> CutPoint:
        """选 kept-suffix 起点（docs/18 Phase 2，含 split-turn）。

        规则：
        - **优先** user MESSAGE 边界：选预算内最近的 user 边界（kept suffix = 它起到 leaf）。
        - **永不** cut at toolResult（悬挂 toolResult 会被 render inverse-orphan 清洗 = 信息丢失）。
        - **split-turn**：若无 user 边界落入预算（即最近 user 起的 suffix 已超预算），且**该 user 之后**
          的内容本身就超预算（超长单 turn——典型如巨大 tool result），则允许 cut 在更靠后的
          assistant/custom/branch_summary/compaction 边界，把 user 问题 + 早段内容压进 summary、只留近段
          ——使 compaction 真正收缩。若超预算只因 user 消息**自身**巨大（其后内容很小），仍兜底保留该
          user（当前问题原文不可摘要掉，docs/16 #10 review）。
        - 每个候选 cut 经 _candidate_suffix_is_renderable（fold→convert_to_llm→render + 无 inverse-orphan）
          验证；不合法 → 退回最近 user 边界。"""
        budget = self.keep_recent_tokens()
        # 全分支最近的 user MESSAGE（always-keep-current-question 锚点，与预算无关）
        last_user, last_user_idx = None, None
        for i in range(len(branch) - 1, -1, -1):
            e = branch[i]
            if e.type == _tree.MESSAGE and (e.data.get("message") or {}).get("role") == "user":
                last_user, last_user_idx = e.id, i
                break
        # 预算内从尾累计：最早的 user 边界 / 最早的合法非 user 边界
        total = 0
        user_cut = None
        split_cut = None
        for e in reversed(branch):
            total += self._entry_token_estimate(e)
            if total > budget:
                break
            if e.type == _tree.MESSAGE and (e.data.get("message") or {}).get("role") == "user":
                user_cut = e.id
            elif self._is_valid_cut_entry(e):
                split_cut = e.id

        def make(cut_id, is_split) -> CutPoint:
            if cut_id is not None and not self._candidate_suffix_is_renderable(branch, cut_id):
                cut_id, is_split = last_user, False     # 验证失败 → 退回最近 user 边界
            return CutPoint(cut_id, self._cut_entry_type(branch, cut_id), bool(is_split and cut_id is not None),
                            self._kept_token_estimate(branch, cut_id))

        if user_cut is not None:
            return make(user_cut, False)
        if last_user_idx is not None:
            post = sum(self._entry_token_estimate(e) for e in branch[last_user_idx + 1:])
            if post > budget and split_cut is not None and split_cut != last_user:
                return make(split_cut, True)            # 超长单 turn 的 post 段 → split-turn
            return make(last_user, False)               # 仅 user 自身超预算 → 保留当前问题
        if split_cut is not None:
            return make(split_cut, True)                # 全分支无 user → 在合法非 user 边界切
        return CutPoint(None, None, False, 0)

    @staticmethod
    def _is_valid_cut_entry(e) -> bool:
        """合法 cut 头：user/assistant MESSAGE、custom_message、compaction、branch_summary。
        绝不含 toolResult（悬挂会被清洗）、亦不含遥测/session_start 等注解型。"""
        if e.type == _tree.MESSAGE:
            return (e.data.get("message") or {}).get("role") in ("user", "assistant")
        return e.type in (_tree.CUSTOM_MESSAGE, _tree.COMPACTION, _tree.BRANCH_SUMMARY)

    @staticmethod
    def _entry_token_estimate(e) -> int:
        """单条 entry 的 token 估计（fold 可见内容）：MESSAGE.content / custom_message.content /
        compaction|branch_summary.summary；其余（遥测/session_start/leaf…）计 0。"""
        from ..context.packs import estimate_tokens
        if e.type == _tree.MESSAGE:
            content = (e.data.get("message") or {}).get("content", "")
        elif e.type == _tree.CUSTOM_MESSAGE:
            content = e.data.get("content", "")
        elif e.type in (_tree.COMPACTION, _tree.BRANCH_SUMMARY):
            content = e.data.get("summary", "")
        else:
            return 0
        return estimate_tokens(content if isinstance(content, (str, list)) else str(content))

    def _candidate_suffix_is_renderable(self, branch, cut_id: "str | None") -> bool:
        """候选 cut 的 kept suffix 是否可合法渲染（docs/18 Phase 2 风险闸）。

        ① kept suffix 内无 inverse-orphan toolResult（toolCallId 在 suffix 内须有对应 toolCall——否则
           render 会静默清掉 = 信息丢失）；② fold→convert_to_llm→render 不抛（provider 合法化通过）。"""
        idx = next((i for i, e in enumerate(branch) if e.id == cut_id), None)
        if idx is None:
            return False
        kept = branch[idx:]
        call_ids: set = set()
        for e in kept:
            if e.type != _tree.MESSAGE:
                continue
            msg = e.data.get("message") or {}
            if msg.get("role") == "assistant":
                for b in (msg.get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        call_ids.add(b.get("id"))
        for e in kept:
            if e.type != _tree.MESSAGE:
                continue
            msg = e.data.get("message") or {}
            if msg.get("role") == "toolResult" and msg.get("toolCallId") not in call_ids:
                return False
        # ② fold→convert_to_llm→render 在 **anthropic 与 openai 两端** 都不抛（Phase 2 §验收：两 provider
        #    都过合法化）；并在 kept 前缀合成一条 compaction summary user 消息——匹配生产 fold 的真实形状
        #    （split-turn 时 suffix 头可能是 assistant，生产里其前总有 summary(user) → 合法）。
        try:
            a = self.agent
            from . import context as _ctx
            from .render import ModelCtx, render
            rich, _ = _ctx.fold(kept)
            neutral = _ctx.convert_to_llm(rich)
            probe = [_tree.user_message("[compaction summary placeholder]")] + neutral
            # deliberately 探两 provider（renderability 对所有 provider 都须成立）：从 SPECS 派生
            # (provider, api, places_system)，in-band system 规则不再硬编码 == "openai"（B1 标志真源）。
            from ..agent.providers import SPECS
            for spec in SPECS.values():
                sysp = a._system_prompt if spec.places_system_in_messages else None
                render(probe, ModelCtx(provider=spec.name, api=spec.capture_api, model_id=a.model),
                       system_prompt=sysp)
        except Exception:
            return False
        return True

    @staticmethod
    def _cut_entry_type(branch, cut_id: "str | None") -> "str | None":
        if cut_id is None:
            return None
        e = next((x for x in branch if x.id == cut_id), None)
        if e is None:
            return None
        return {_tree.MESSAGE: "message", _tree.CUSTOM_MESSAGE: "custom_message",
                _tree.COMPACTION: "compaction", _tree.BRANCH_SUMMARY: "branch_summary"}.get(e.type)

    def _kept_token_estimate(self, branch, cut_id: "str | None") -> int:
        if cut_id is None:
            return 0
        idx = next((i for i, e in enumerate(branch) if e.id == cut_id), None)
        if idx is None:
            return 0
        return sum(self._entry_token_estimate(e) for e in branch[idx:])

    def _project_branch_prefix(self, prefix_entries) -> "list | None":
        """branch 前缀 → provider-shaped 消息（summarizer 输入）。与请求渲染同一管线
        （fold→convert_to_llm→render），summarizer 看到的 = 模型曾看到的前缀。空前缀 → None。"""
        if not prefix_entries:
            return None
        a = self.agent
        from . import context as _ctx
        from .render import ModelCtx, render
        rich, _ = _ctx.fold(list(prefix_entries))
        neutral = _ctx.convert_to_llm(rich)
        if not neutral:
            return None
        sysp = a._system_prompt if a._provider.places_system_in_messages else None
        return render(neutral, ModelCtx(provider=a._provider.name, api=a._provider.capture_api,
                                        model_id=a.model),
                      system_prompt=sysp)["messages"]

    def _predicted_post_compaction_count(self, summary: str, first_kept: "str | None") -> "int | None":
        """预测 compaction entry 落树后的 neutral 消息数（entry 是 append-only，写后不可补字段，
        故 messageCountAfter 须在写前算）：对 branch + 合成 pending compaction entry 跑**真实** fold
        （不手写两区逻辑，与 build_context 永远一致）。"""
        a = self.agent
        mgr = a._session_mgr
        if mgr is None:
            return None
        from . import context as _ctx
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
            provider=a._provider.name,
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
                    message_count_after=event.message_count_after,
                    details=event.details)
                return True
            except Exception as e:
                a.emit(_events.NoticeRaised(text=f"[tree] compaction entry append failed: {e}"))
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
                a.emit(_events.NoticeRaised(text=f"[tree] record_event failed: {e}"))
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
            a.emit(_events.NoticeRaised(text=f"[tree] telemetry append failed ({entry_type}): {e}"))

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
            a.emit(_events.NoticeRaised(text=f"[tree] custom_message append failed ({custom_type}): {e}"))
            return False

    # ── 请求构建（docs/13 S2；#3b 自 engine 迁入）────────────────────────────────
    def build_request_messages(self, extra_neutral: "list[dict] | None" = None) -> list:
        """从 canonical 树渲染本轮请求（`render(build_context())`）。

        树是会话**唯一**事实源（含 message-end 写入的消息 + 注入的 custom_message）；render 据当前
        provider 整形 + 合并相邻 user。**无 flat fallback**：缺 writer lease 即 fatal（lease prologue
        已在 turn 开始保证 _session_mgr 存在）。Anthropic system 走 out-of-band，OpenAI system 经
        render 注入 index 0。"""
        a = self.agent
        if a._session_mgr is None:
            raise _tree.SessionTreeError("no writer lease: cannot build request messages without canonical tree")
        from .render import ModelCtx, render
        provider = a._provider.name
        api = a._provider.capture_api
        sysp = a._system_prompt if a._provider.places_system_in_messages else None
        built = a._session_mgr.build_context()
        messages = list(built.messages)
        if extra_neutral:
            messages.extend(extra_neutral)     # volatile tail（request-local 装饰，不入树，docs/16 #6）
        # docs/18 Phase 6：per-message 聚合 tool-result 预算（请求局部，render 前）。决策按 toolCallId
        # 冻结复用 → prompt-cache 前缀稳定；只动 toolResult 组、不碰 volatile tail / 树。
        # 仅 Anthropic：其 render 把连续 toolResult 并成**一条** user message（聚合上限有意义）；OpenAI
        # 每个 toolResult 自成一条 tool message（无聚合上限），施加聚合预算会过度删减（review）。
        state = getattr(a, "_content_replacement", None)
        if state is not None and a._provider.name == "anthropic":
            from ..agent.tool_result_budget import apply_tool_result_budget
            messages = apply_tool_result_budget(
                messages, state,
                per_group_token_budget=max(8000, int(a.effective_window * 0.1)))
        return render(messages, ModelCtx(provider=provider, api=api, model_id=a.model),
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
        services = getattr(a, "_runtime_services", None)
        cwd = services.cwd if services is not None else os.getcwd()
        req = ContextRequest(
            cwd=cwd, is_sub_agent=False,
            include_project_instructions=True, include_memory=True,
            include_env=False, include_git=False, include_skills=False,
            include_agents=False, include_deferred_tools=False,
        )
        try:
            plan = await ContextRuntime(
                budget=BudgetPolicy.for_window(a.effective_window),
                sources=(services.context_sources if services is not None else None),
            ).collect(req)
        except Exception as e:
            a.emit(_events.NoticeRaised(text=f"[context] session-context collect failed: {e}"))
            return
        for pack in plan.packs:
            if pack.kind not in present:                  # per-customType：只注入缺失的 kind
                ok = a.emit(_events.ContextInjected(custom_type=pack.kind, content=pack.content))
                self._ledger_note(pack, ok)
            else:
                self._ledger_note(pack, True, reason="already rendered in context (skip re-inject)")

    def _session_context_present_kinds(self) -> set[str]:
        """当前 branch **经 context.fold 实际渲染**的 session-context customType 集合。

        用 fold（而非手算 post-compaction 区间）确保与真正发给模型的上下文一致：fold 的 rich 输出保留
        custom_message 的 customType（convert_to_llm 才丢弃）,故能精确判定「模型是否看得到该包」。"""
        mgr = self.agent._session_mgr                     # lease 先行保证非 None（docs/16 C-1）
        try:
            from . import context as _ctx
            rich, _ = _ctx.fold(mgr.get_branch())
        except Exception:
            return set(self._SESSION_CONTEXT_KINDS)       # 树不可重建 → 保守视为全在(不重复注入)
        return {m.get("customType") for m in rich
                if m.get("role") == "custom" and m.get("customType") in self._SESSION_CONTEXT_KINDS}

    def _ledger_note(self, pack, included: bool, reason: str = "") -> None:
        """把一次注入记进本 turn 的全量账本（/context 可见性，docs/16 #6）。无账本（turn 外直调）→ no-op。"""
        led = getattr(self.agent, "_context_ledger", None)
        if led is not None:
            led.add(pack, included=included,
                    reason=reason or ("injected" if included else "tree write failed; will retry"))

    def inject_skill_listing(self) -> None:
        """skill 清单（docs/16 #6 provider 化：SkillListingProvider，lifecycle=until_compact）。
        dedup（_sent_skill_names）**只在树写成功后** commit——失败则下一轮重试，不静默丢清单。"""
        a = self.agent
        if a.is_sub_agent:
            return
        from ..context.providers import SkillListingProvider
        provider = SkillListingProvider(a)
        pack = provider.collect()
        if pack is None:
            return
        ok = a.emit(_events.ContextInjected(custom_type=pack.kind, content=pack.content))
        if ok:
            provider.commit()
        self._ledger_note(pack, ok)

    def inject_pending_skill_bodies(self) -> None:
        """skill body（docs/16 #6 provider 化：skill_body_pack，lifecycle=one_shot）。
        树写失败的 body 留在队列、下一轮重试（不静默丢指令）。"""
        a = self.agent
        from ..context.providers import skill_body_pack
        remaining: list = []
        for name, body in a._pending_skill_bodies:
            pack = skill_body_pack(name, body)
            ok = a.emit(_events.ContextInjected(custom_type=pack.kind, content=pack.content))
            if not ok:
                remaining.append((name, body))
            self._ledger_note(pack, ok, reason=("injected" if ok else "tree write failed; requeued"))
        a._pending_skill_bodies = remaining

    def inject_finished_tasks(self) -> None:
        """终态后台任务提醒（docs/16 #6 provider 化：FinishedTasksProvider，lifecycle=turn）。
        custom_message 挂在 **live leaf**（必须在当前 branch 上，否则模型看不到完成提醒——这优先于
        docs/14 §6b 的"pin 到 spawn 分支"）；spawn 血缘记在 task.spawn_entry_id 供审计。
        树写失败 → 不 commit（不标 injected）、下一轮重试，不静默丢提醒。"""
        a = self.agent
        if a.is_sub_agent:
            return   # 子 agent 与父共享 TaskManager；finished-task 回注是**父**（user-facing loop）的职责，
                     # 否则子会"偷走"并标 injected 父/兄弟的后台完成提醒，使父永不浮现（review high）。
        from ..context.providers import FinishedTasksProvider
        provider = FinishedTasksProvider(a)
        pack = provider.collect()
        if pack is None:
            return
        ok = a.emit(_events.ContextInjected(custom_type=pack.kind, content=pack.content))
        if ok:
            provider.commit()
        self._ledger_note(pack, ok)

    # ── /clear 与 derived-cache 落盘（docs/16 #3b 自 engine 迁入）────────────────
    def clear_history(self) -> None:
        """docs/14 SessionLease：/clear = 把 active leaf 复位到 root（in-file），而非清空对话事实。
        历史保留在 canonical 树里（可经 /tree 回看 / 在旧分支继续）；后续 turn 从 root 起一条新分支
        （每请求都从树重渲染，无需装载 flat 投影——flat 列表已退役，docs/16 #3c）。
        复位本对话的 working set + 计数。"""
        a = self.agent
        a._ensure_session_lease()
        a._session_mgr.set_leaf(None)        # 回到 root：get_branch(None)==[] → 空上下文
        a.total_input_tokens = 0
        a.total_output_tokens = 0
        a.last_input_token_count = 0
        a._sent_skill_names = set()
        a._pending_skill_bodies = []
        a._activated_path_skills = set()
        a._active_hooks = []
        # review：/clear 起一条全新逻辑对话（leaf→root）——上一分支的 auto-compaction 失败计数不应
        # 把新对话的自动压缩永久熔断；tool-result 替换决策也属旧分支，一并复位。
        a._consecutive_compaction_failures = 0
        from ..agent.tool_result_budget import ContentReplacementState
        a._content_replacement = ContentReplacementState()
        from ..skills.discovery import reset_skill_cache
        reset_skill_cache()
        a.emit(_events.NoticeRaised(text="Conversation cleared (leaf reset to root; history kept — /tree to revisit)."))

    def clear_for_plan_execution(self) -> None:
        """plan clear-and-execute（docs/16 #3c，取代 flat 的 _clear_history_keep_system +
        _context_cleared）：leaf 复位 root + 发 turn 内 context-break 信号——loop 在当前工具结果处
        消费信号，把结果作为新分支首条 user 消息落树（plan 内容随之入新上下文）。

        修复 latent bug：旧 flat clear 自 docs/13 S2 起从未生效（请求从树渲染，flat 清空被下一次
        重渲染覆盖，历史原样回流）；leaf 复位才是树语义下真正的 clear。"""
        a = self.agent
        if a._session_mgr is not None:
            a._session_mgr.set_leaf(None)
        a.last_input_token_count = 0
        a._pending_context_break = True

    def auto_save(self) -> None:
        """v2 state.json（host task 派生 cache）按需落盘——canonical 树是 resume 权威。"""
        a = self.agent
        from . import v2 as _session_v2
        if _session_v2.is_v2_session(a.session_id) or a.task_manager.list_tasks():
            a._persist_state()

    # ── in-file 导航（docs/14 P6）────────────────────────────────────────────
    def _abandoned_branch_entries(self, from_leaf_id: "str | None",
                                  to_leaf_id: "str | None") -> list:
        """Entries on the current branch that would be left behind by moving to target."""
        mgr = self.agent._session_mgr
        if mgr is None or from_leaf_id == to_leaf_id:
            return []
        try:
            current = mgr.get_branch(from_leaf_id)
            target = mgr.get_branch(to_leaf_id) if to_leaf_id is not None else []
        except Exception:
            return []
        target_ids = {e.id for e in target}
        common_idx = -1
        for i, e in enumerate(current):
            if e.id in target_ids:
                common_idx = i
        return current[common_idx + 1:]

    def branch_summary_available(self, target_id: "str | None") -> bool:
        """Whether moving to target would abandon summarizable branch context."""
        mgr = self.agent._session_mgr
        if mgr is None:
            return False
        abandoned = self._abandoned_branch_entries(mgr.get_leaf(), target_id)
        return any(e.type in (_tree.MESSAGE, _tree.CUSTOM_MESSAGE, _tree.COMPACTION, _tree.BRANCH_SUMMARY)
                   for e in abandoned)

    def _summary_text_for_entries(self, entries: list) -> str:
        """Plain transcript for a branch-summary LLM request（兼容入口；Phase 7 收敛到
        branch_summary.serialize_branch_conversation——带 tool-result 限长）。"""
        from . import branch_summary as _bs
        return _bs.serialize_branch_conversation(entries)

    async def move_to_with_branch_summary(self, target_id: "str | None",
                                          *, focus: "str | None" = None) -> list:
        """切到 target，并把被离开 branch 收敛成 branch_summary entry（docs/18 Phase 7，Pi 语义）。

        deepest common ancestor 收集 abandoned → 最新到最旧 token 预算装入 → 限长 transcript →
        结构化 branch_summary_prompt 摘要 → append_branch_summary（挂 target、成新 leaf，details 携
        readFiles/modifiedFiles/commonAncestorId/sourceLeafId/targetId/messageCount/focus）。无可摘要
        内容 / 空 transcript / summary 为空 → 退回纯 move_to(target)。file tracking 对**全部** abandoned
        累计（即使部分因预算没进 summarizer 输入）。"""
        from . import branch_summary as _bs
        from .render import ModelCtx, render
        from ..agent.summary_prompts import branch_summary_prompt, format_compact_summary

        a = self.agent
        mgr = a._session_mgr
        if mgr is None:
            raise ValueError("no active session writer lease; cannot navigate the tree")
        if target_id is not None and target_id not in {e.id for e in mgr.entries()}:
            raise ValueError(f"entry '{target_id}' not found in session tree; session left unchanged")

        old_leaf = mgr.get_leaf()
        abandoned, common_ancestor_id = _bs.collect_entries_for_branch_summary(mgr, old_leaf, target_id)
        if not any(e.type in (_tree.MESSAGE, _tree.CUSTOM_MESSAGE, _tree.COMPACTION, _tree.BRANCH_SUMMARY)
                   for e in abandoned):
            return self.move_to(target_id)
        budgeted = _bs.prepare_branch_entries(abandoned, self.keep_recent_tokens())
        transcript = _bs.serialize_branch_conversation(budgeted)
        if not transcript.strip():
            return self.move_to(target_id)

        prompt = branch_summary_prompt(focus)
        messages = [{"role": "user", "content": "Abandoned branch transcript:\n\n" + transcript}]
        summary = await a._summarize(messages, prompt)
        summary = format_compact_summary(summary) if summary else summary
        if not summary:
            return self.move_to(target_id)

        read_files, modified_files = _bs.branch_file_tracking(abandoned)
        details = {
            "readFiles": read_files, "modifiedFiles": modified_files,
            "commonAncestorId": common_ancestor_id, "sourceLeafId": old_leaf,
            "targetId": target_id, "messageCount": len(abandoned),
        }
        if focus:
            details["focus"] = focus
        # review HIGH/edge：target_id=None（切到 root）时，单次原子写入把 branch_summary 直接挂 root
        # （attach_to_root），绝不先 set_leaf(None) 再 append——否则 append 若抛出，leaf 已移到 root 却无
        # summary，旧 branch 被孤立丢失（无回滚）。branch_summary 本身推进 leaf，故无需额外 set_leaf。
        mgr.append_branch_summary(summary=summary, from_id=old_leaf, details=details,
                                  parent_id=target_id, attach_to_root=(target_id is None))
        built = mgr.build_context()
        provider = a._provider.name
        api = a._provider.capture_api
        sysp = a._system_prompt if a._provider.places_system_in_messages else None
        return render(built.messages, ModelCtx(provider=provider, api=api, model_id=a.model),
                      system_prompt=sysp)["messages"]

    def move_to(self, entry_id: "str | None", *, agent_id: str = "main") -> list:
        """把 active leaf 移到 canonical 树的 entry_id（in-file 导航：/tree <entry> /checkout /rewind）。
        entry_id=None → 复位 root（空上下文）。fail-closed：无写者租约 / entry 不存在 → ValueError。
        返回从新 leaf 重渲染的消息列表（供调用方显示计数；请求路径每轮自行重渲染，无需装载）。"""
        from .render import ModelCtx, render
        a = self.agent
        mgr = a._session_mgr
        if mgr is None:
            raise ValueError("no active session writer lease; cannot navigate the tree")
        if entry_id is not None and entry_id not in {e.id for e in mgr.entries()}:
            raise ValueError(f"entry '{entry_id}' not found in session tree; session left unchanged")
        mgr.set_leaf(entry_id)
        built = mgr.build_context()
        provider = a._provider.name
        api = a._provider.capture_api
        sysp = a._system_prompt if a._provider.places_system_in_messages else None
        return render(built.messages, ModelCtx(provider=provider, api=api, model_id=a.model),
                      system_prompt=sysp)["messages"]

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
