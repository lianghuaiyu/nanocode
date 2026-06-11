"""P4：AgentRuntime / RuntimeThread / TurnResult / ApprovalManager —— in-process facade。

把 CLI 直接驱动 Agent 的散点（chat / restore / abort / confirm_fn / plan_approval_fn /
token 计数）收敛到一个稳定的、面向外部调用方的句柄。本步**仅 in-process、无 server、无
协议**；行为不变。RuntimeThread 包住 AgentSession（P3），AgentRuntime 管 thread 生命周期。

不可回归契约（见 P3+P4 测绘）：
- cancel 必须委托 agent.abort()（先置 _aborted 再 cancel task），否则后端循环/子 agent
  超时判别会回归。
- chat() 把取消吞成 _aborted=True 并正常返回（不抛）；故 TurnResult.status 必须在 run()
  await 之后读 agent._aborted 来映射 cancelled，绝不能把正常返回当 success。
- ApprovalManager 同时包 confirm_fn(bool) 与 plan_approval_fn(dict) 两个不同契约；保留
  按身份装饰 + 去重 + 后台 fail-closed（这些在 Agent 内，manager 只是注入点的归口）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from .session import AgentSession
from .sink import BufferSink, TeeSink


# ─── 结果对象 ────────────────────────────────────────────────

@dataclass
class TurnResult:
    """一个 turn 的结构化结果（RuntimeThread.run 返回）。"""
    session_id: str
    thread_id: str
    status: Literal["completed", "cancelled"]
    final_response: str
    input_tokens: int
    output_tokens: int
    error: str | None = None


@dataclass
class AgentResult:
    """子 agent / 分支线程的结构化结果（形式化现有 _build_agent_result dict）。

    host-derived 不变量：files_read/files_modified 由宿主观测，绝不信任模型自述。
    """
    agent_id: str
    branch_id: str
    status: str
    summary: str
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    result_path: str | None = None
    messages_path: str | None = None
    events_path: str | None = None
    tokens: dict = field(default_factory=dict)


# ─── Bootstrap 配置 ───────────────────────────────────────────

@dataclass
class AgentConfig:
    """构造一个主 Agent 所需的全部配置（docs/14 §3.1）——单一 bootstrap 数据载体，使 CLI / SDK /
    未来 AppServer 经 `AgentRuntime.thread_start(config)` 共用一条构造路径，不各自重造 Agent。

    刻意只是数据 + `build_agent()`：CLI 侧的 I/O（trust gate、memory backend 选择、resume 目标
    解析）仍留在 cli.main()，把解析结果灌进本 config，再交 runtime 构造——既共享构造、又不把
    交互 I/O 拖进 runtime。"""
    permission_mode: str = "default"
    model: str = "claude-opus-4-6"
    thinking: bool = False
    max_cost_usd: float | None = None
    max_turns: int | None = None
    api_key: str | None = None
    api_base: str | None = None            # openai-compatible base（非空 → use_openai）
    anthropic_base_url: str | None = None
    trace_enabled: bool = False
    trajectory_enabled: bool = False
    trajectory_level: str = "summary"
    workspace_trusted: bool = True
    memory_backend: object | None = None
    session_id: str | None = None          # resume adopt 目标（None = 新 mint）
    sink: object | None = None

    def build_agent(self):
        from .engine import Agent
        return Agent(
            permission_mode=self.permission_mode, model=self.model, thinking=self.thinking,
            max_cost_usd=self.max_cost_usd, max_turns=self.max_turns,
            api_base=self.api_base, anthropic_base_url=self.anthropic_base_url, api_key=self.api_key,
            trace_enabled=self.trace_enabled, trajectory_enabled=self.trajectory_enabled,
            trajectory_level=self.trajectory_level, workspace_trusted=self.workspace_trusted,
            session_id=self.session_id, memory_backend=self.memory_backend, sink=self.sink,
        )


# ─── 审批归口 ────────────────────────────────────────────────

ConfirmFn = Callable[[str], Awaitable[bool]]
PlanApprovalFn = Callable[[str], Awaitable[dict]]


class ApprovalManager:
    """两条审批通道的注入归口：confirm_fn(bool) + plan_approval_fn(dict)。

    本身不实现去重/身份装饰/fail-closed——那些是 Agent 内的不变量（_confirm_if_needed /
    _decorate_confirm_message / _auto_deny_confirm），manager 只负责把外部 handler 装到
    agent 的两个回调槽，使 RuntimeThread/外部调用方有单一接线点。未注入时保持 agent 默认
    （confirm_fn=None → 阻塞 input() 回退；plan_approval_fn=None → manual-execute）。
    """

    def __init__(self, *, confirm_fn: "ConfirmFn | None" = None,
                 plan_approval_fn: "PlanApprovalFn | None" = None) -> None:
        self.confirm_fn = confirm_fn
        self.plan_approval_fn = plan_approval_fn

    def attach(self, agent) -> None:
        if self.confirm_fn is not None:
            agent.set_confirm_fn(self.confirm_fn)
        if self.plan_approval_fn is not None:
            agent.set_plan_approval_fn(self.plan_approval_fn)


# ─── 线程句柄 ────────────────────────────────────────────────

class RuntimeThread:
    """外部 API 面向的会话句柄：run→TurnResult、cancel、token 计数。

    包住 AgentSession（P3）；in-process。final_response：主线程经注入的 capturing
    BufferSink 取回（不回归 TerminalSink 打印——见 AgentRuntime.thread_start 的 sink 决策）。
    """

    def __init__(self, runtime: "AgentRuntime", agent, session: AgentSession,
                 *, capture: "BufferSink | None" = None) -> None:
        self._runtime = runtime
        self.agent = agent
        self.session = session
        self._capture = capture  # 若注入则用于 final_response 捕获
        self._disposed = False
        # thread 身份在构造时**快照** agent.session_id：in-place rebind（docs/14 P2）会原地改
        # agent.session_id，若此处用 live property，old/new thread（复用同一 Agent）的 thread_id
        # 会同时变成 new sid，dispose(old) 误注销 new。快照保证 thread 身份稳定、注册/注销不串。
        self._thread_id = agent.session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def is_processing(self) -> bool:
        return self.agent.is_processing

    async def run(self, prompt: str) -> TurnResult:
        if self._disposed:
            # 被 dispose 的 thread 复用同一 Agent，但 Agent 可能已 rebind 到别的 session；
            # 让 stale 句柄驱动会写错 session（codex B2）。inert 化：拒绝运行。
            raise RuntimeError("RuntimeThread is disposed; obtain the current thread from the host")
        if self._capture is not None and hasattr(self._capture, "reset"):
            self._capture.reset()  # 每 turn 重置捕获，避免跨 turn 累积
        prev_in = self.agent.total_input_tokens
        prev_out = self.agent.total_output_tokens
        await self.session.run_turn(prompt)
        # 取消语义：chat() 把取消吞成 _aborted 并正常返回——必须在此 await 之后读 _aborted。
        status: "Literal['completed','cancelled']" = "cancelled" if self.agent._aborted else "completed"
        final = self._capture.text() if self._capture is not None and hasattr(self._capture, "text") else ""
        return TurnResult(
            session_id=self.agent.session_id,
            thread_id=self.thread_id,
            status=status,
            final_response=final,
            input_tokens=self.agent.total_input_tokens - prev_in,
            output_tokens=self.agent.total_output_tokens - prev_out,
        )

    def cancel(self) -> None:
        """委托 agent.abort()（先置 _aborted 再 cancel task）——保留优雅取消契约。"""
        if self._disposed:
            return
        self.agent.abort()

    def dispose(self) -> None:
        """从 runtime registry 注销本 thread、置 disposed（使 run/cancel inert）并丢弃 capture（docs/14 P1）。

        **不** finalize tracer / 不动 Agent —— 那是 Agent.rebind_session（P2）的职责。in-place
        rebind 下 old/new thread 复用同一 Agent，dispose 只做 wrapper 级清理；unregister 按
        **对象身份** compare-and-delete，即便 old/new 共享同一 session_id 也不会误删 new。"""
        self._disposed = True
        self._runtime.unregister(self)
        self._capture = None

    def tokens(self) -> dict:
        return self.agent.get_token_usage()


# ─── Runtime ────────────────────────────────────────────────

class AgentRuntime:
    """Codex 化 in-process facade：管 thread 生命周期。

    本步只对接已构造的 Agent（CLI 仍负责 Agent 构造/配置）；thread_start/resume 把它包成
    RuntimeThread。thread_fork / 协议 / server 留待 P5 / P6。
    """

    def __init__(self) -> None:
        self._threads: dict[str, RuntimeThread] = {}

    def adopt(self, agent, *, approvals: "ApprovalManager | None" = None,
              capture_response: bool = False) -> RuntimeThread:
        """把一个已构造的 Agent 纳管为 RuntimeThread（in-process 起点）。

        capture_response=True 时，给主 agent 的 sink 外挂一个 BufferSink（经 TeeSink 与现有
        显示 sink 并存），使 TurnResult.final_response 能取回助手文本而不影响终端打印。
        """
        if approvals is not None:
            approvals.attach(agent)
        capture: "BufferSink | None" = None
        if capture_response:
            capture = BufferSink()
            agent._sink = TeeSink(agent._sink, capture)
        session = AgentSession(agent)
        thread = RuntimeThread(self, agent, session, capture=capture)
        return self.register(thread)

    def thread_start(self, config: "AgentConfig", *, approvals: "ApprovalManager | None" = None,
                     capture_response: bool = False) -> RuntimeThread:
        """从 AgentConfig 构造一个全新 Agent 并 adopt 为首个 thread（docs/14 §3.1）。

        CLI / SDK / AppServer 的统一入口：config.build_agent() 造 Agent，再 adopt（attach 审批 +
        可选 capture + 注册）。live 切换（/new /resume…）走 thread_new/thread_resume（in-place
        rebind），不经此路径——thread_start 仅用于"开新一个 runtime 上的首 thread"。"""
        return self.adopt(config.build_agent(), approvals=approvals, capture_response=capture_response)

    def register(self, thread: "RuntimeThread") -> RuntimeThread:
        """把 thread 纳入 registry（按 thread_id）。adopt / runtime 构造器（P2）/ host.replace_thread 共用入口。"""
        self._threads[thread.thread_id] = thread
        return thread

    def unregister(self, thread: "RuntimeThread") -> None:
        """compare-and-delete by identity：仅当该 slot 仍指向 thread 本身才移除（幂等）。

        若 old/new 共享同一 session_id（未来 same-sid rebind / 会话重入），register(new) 已先覆盖
        slot，此处 old.dispose()→unregister(old) 见 slot 是 new、不删——保证 registry 不丢当前 thread。"""
        tid = thread._thread_id
        if self._threads.get(tid) is thread:
            self._threads.pop(tid, None)

    def thread(self, thread_id: str) -> "RuntimeThread | None":
        return self._threads.get(thread_id)

    def threads(self) -> list[RuntimeThread]:
        return list(self._threads.values())

    # ─── 生命周期替换：live switch via in-place rebind（docs/14 P2）────────────────

    def _switch_via_rebind(self, host, new_sid: str, *,
                           parent_session: "dict | None" = None) -> RuntimeThread:
        """把 host 当前 thread 的 Agent 原地 rebind 到 new_sid，建新 AgentSession+RuntimeThread
        包**同一** agent，经 host.replace_thread 切入（register 新 + dispose 旧）。返回新 thread。

        调用方负责先过 host.can_switch() fail-closed 闸；本方法不重复判（但 rebind 自身对
        sub-agent / same-sid 有保护）。新建的 AgentSession 重新绑 agent 当前 session_id，
        因此修复了 unsafe-switch 的 stale context_builder 问题。"""
        agent = host.current_thread.agent
        agent.rebind_session(new_sid, parent_session=parent_session)
        new_thread = RuntimeThread(self, agent, AgentSession(agent),
                                   capture=host.current_thread._capture)
        host.replace_thread(new_thread)
        return new_thread

    def thread_new(self, host) -> RuntimeThread:
        """新建空 canonical session 并切入（/new）。session_id 与主 agent 同格式（8 hex）；
        re-mint 直到不与磁盘上已有 session 撞——否则 rebind 会 open 到陌生 session 而非建空的
        （违反 /new "新 session context 为空"，docs/14 P2 review）。"""
        from ..session.manager import SessionManager
        sid = uuid.uuid4().hex[:8]
        while SessionManager.exists(sid):
            sid = uuid.uuid4().hex[:8]
        return self._switch_via_rebind(host, sid)

    def thread_resume(self, host, session_id: str) -> "RuntimeThread | None":
        """切到一个已存在的 session（/resume <id>）。无 canonical 树时尝试 legacy 迁移；
        迁移仍不出可用树 → 返回 None（调用方提示失败、保留当前 thread）。"""
        from ..session.manager import SessionManager
        if not SessionManager.exists(session_id):
            from ..session.migration import migrate_session
            rep = migrate_session(session_id)
            if rep.get("status") not in ("migrated", "skipped") or not SessionManager.exists(session_id):
                return None
        return self._switch_via_rebind(host, session_id)

    def thread_clone(self, host, source_sid: str, entry_id: "str | None" = None) -> "RuntimeThread | None":
        """跨文件 clone（/clone [entry]）：复制 source 的 path-to-root 到新 session（header 记
        parentSession 血缘）并切入。源无 canonical 树 / clone 失败 → None。"""
        from ..session.manager import SessionManager
        if not SessionManager.exists(source_sid):
            return None
        try:
            child = SessionManager.open(source_sid).clone(entry_id)
        except Exception:
            return None
        return self._switch_via_rebind(host, child.session_id)

    def thread_fork(self, host, source_sid: str, selected_entry_id: str) -> "tuple[RuntimeThread | None, str | None]":
        """Pi before-user fork（/fork）：在 **选中 user 消息之前** 重新开始。fork point = 该 user
        消息的 parent；新 session = clone(fork_point)（不含选中消息及其后），返回 (新 thread, 选中文本)
        ——选中文本回给调用方供用户重新编辑/发送。selected 无效 → (None, None)。"""
        from ..session.manager import SessionManager
        from ..session import tree as _tree
        if not SessionManager.exists(source_sid):
            return None, None
        src = SessionManager.open(source_sid)
        sel = next((e for e in src.entries() if e.id == selected_entry_id), None)
        if sel is None:
            return None, None
        selected_text = (sel.data.get("message") or {}).get("content") if sel.type == _tree.MESSAGE else None
        fork_point = sel.parentId
        # pre-3a 盘上树：首消息 parentId 指向 session_start（header）→ 视同 None（fork 到空）。
        if fork_point is not None:
            fp = next((e for e in src.entries() if e.id == fork_point), None)
            if fp is not None and fp.type == _tree.SESSION_START:
                fork_point = None
        try:
            if fork_point is None:
                # 选中是 branch root（第一条消息）→ fork 到空 session（其前无内容）
                child = SessionManager.create(parent_session={"sessionId": source_sid, "entryId": None})
            else:
                child = src.clone(fork_point)
        except Exception:
            return None, None
        return self._switch_via_rebind(host, child.session_id), selected_text
