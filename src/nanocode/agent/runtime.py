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

    @property
    def thread_id(self) -> str:
        return self.agent.session_id

    @property
    def is_processing(self) -> bool:
        return self.agent.is_processing

    async def run(self, prompt: str) -> TurnResult:
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
        self.agent.abort()

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
        self._threads[thread.thread_id] = thread
        return thread

    def thread(self, thread_id: str) -> "RuntimeThread | None":
        return self._threads.get(thread_id)

    def threads(self) -> list[RuntimeThread]:
        return list(self._threads.values())
