"""runtime/facade.py — AgentRuntime / RuntimeThread / ApprovalManager in-process facade.

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

import asyncio
import os
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from ..session.agent import AgentSession


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


@dataclass(frozen=True)
class SkillInvocation:
    """Result of a user-invoked skill crossing the runtime boundary."""

    handled: bool
    prompt: str | None = None
    notice: str | None = None
    error: str | None = None


class ReadOnlySessionView:
    """Read-only projection of the active canonical session tree."""

    def __init__(self, manager) -> None:
        self._manager = manager
        self.session_id = manager.session_id

    def entries(self):
        return self._manager.entries()

    def get_leaf(self):
        return self._manager.get_leaf()

    def get_branch(self, leaf_id: str | None = None):
        return self._manager.get_branch(leaf_id)

    def build_context(self, leaf_id: str | None = None):
        return self._manager.build_context(leaf_id)

    def labels(self) -> dict[str, str]:
        return self._manager.labels()

    def name(self) -> str | None:
        return self._manager.name()

    def parent_session(self) -> dict | None:
        return self._manager.parent_session()

    def _cwd(self) -> str:
        return self._manager._cwd()


def _jsonable(value: Any) -> Any:
    """Convert runtime boundary values to JSON-able Python containers."""
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def serialize_event_envelope(env: dict) -> dict:
    """Public runtime event schema: always JSON-able at the facade boundary."""
    out = dict(env)
    out["event"] = _jsonable(out.get("event"))
    return out


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
    trajectory_enabled: bool = False
    trajectory_level: str = "summary"
    workspace_trusted: bool = True
    memory_backend: object | None = None
    memory_backend_choice: str | None = None
    session_id: str | None = None          # resume adopt 目标（None = 新 mint）
    cwd: str | None = None
    # docs/19：public sandbox profile（default/read-only/strict/vm/danger-full-access）。
    # 投影为 SandboxPolicy；public API 只暴露 profile，不暴露 adapter argv / msb / mount。
    sandbox_profile: str = "default"

    def build_agent(self):
        from ..agent.engine import Agent
        return Agent(
            permission_mode=self.permission_mode, model=self.model, thinking=self.thinking,
            max_cost_usd=self.max_cost_usd, max_turns=self.max_turns,
            api_base=self.api_base, anthropic_base_url=self.anthropic_base_url, api_key=self.api_key,
            trajectory_enabled=self.trajectory_enabled,
            trajectory_level=self.trajectory_level, workspace_trusted=self.workspace_trusted,
            session_id=self.session_id, memory_backend=self.memory_backend,
            sandbox_profile=self.sandbox_profile,
        )


@contextmanager
def _push_cwd(cwd: str):
    old = os.getcwd()
    if old == cwd:
        yield
        return
    os.chdir(cwd)
    try:
        yield
    finally:
        os.chdir(old)


@dataclass(frozen=True)
class RuntimeServices:
    """Cwd-bound services owned by the runtime host."""

    cwd: str
    agent_dir: str
    workspace_trusted: bool
    memory_backend: object | None
    context_sources: object
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def create(cls, config: AgentConfig, *, cwd: str | None = None) -> "RuntimeServices":
        resolved = str(Path(cwd or config.cwd or os.getcwd()).resolve())
        from ..context import ContextSources
        from ..paths import data_dir
        from ..trust import is_trusted

        def _in_cwd(fn):
            def _wrapped(request):
                with _push_cwd(resolved):
                    return fn(request)
            return _wrapped

        def _git(_request):
            from ..prompt import get_git_context
            return get_git_context()

        def _project(_request):
            from ..prompt import load_project_instructions
            return load_project_instructions()

        def _memory(_request):
            from ..memory import build_memory_prompt_section
            return build_memory_prompt_section()

        diagnostics: list[str] = []
        backend = config.memory_backend
        if backend is None:
            try:
                from ..memory import select_backend
                with _push_cwd(resolved):
                    backend = select_backend(config.memory_backend_choice)
            except Exception as e:
                backend = None
                diagnostics.append(f"memory backend unavailable: {e}")

        trusted = config.workspace_trusted if str(Path(config.cwd or resolved).resolve()) == resolved else is_trusted(Path(resolved))
        return cls(
            cwd=resolved,
            agent_dir=str(data_dir()),
            workspace_trusted=trusted,
            memory_backend=backend,
            context_sources=ContextSources(
                git=_in_cwd(_git),
                project_instructions=_in_cwd(_project),
                memory_static=_in_cwd(_memory),
            ),
            diagnostics=tuple(diagnostics),
        )


def _apply_runtime_services(agent, services: RuntimeServices) -> None:
    agent._runtime_services = services
    agent._memory_backend = services.memory_backend
    agent.workspace_trusted = services.workspace_trusted
    with _push_cwd(services.cwd):
        from ..prompt import build_system_prompt
        agent._base_system_prompt = build_system_prompt()
    agent._apply_permission_mode_prompt()


# ─── 审批归口 ────────────────────────────────────────────────

ConfirmFn = Callable[[str], Awaitable[bool]]
PlanApprovalFn = Callable[[str], Awaitable[dict]]


@dataclass(frozen=True)
class ApprovalRequest:
    """Runtime/UI approval protocol message."""

    request_id: str
    kind: str
    message: str
    metadata: dict = field(default_factory=dict)
    timeout_ms: int | None = None


class RuntimeApprovalBroker:
    """Small request/response broker for mode adapters such as RPC/TUI."""

    def __init__(self, *, emit: Callable[[dict], None], default: bool = False) -> None:
        self._emit = emit
        self._default = default
        self._seq = 0
        self._pending: dict[str, asyncio.Future] = {}

    async def confirm(self, message: str, *, kind: str = "confirm",
                      metadata: dict | None = None, timeout_ms: int | None = None) -> bool:
        self._seq += 1
        rid = f"appr-{self._seq}"
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[rid] = fut
        req = ApprovalRequest(rid, kind, message, metadata or {}, timeout_ms)
        self._emit({"type": "approval_request", **_jsonable(req)})
        try:
            if timeout_ms is None:
                return bool(await fut)
            return bool(await asyncio.wait_for(fut, timeout=timeout_ms / 1000))
        except asyncio.TimeoutError:
            return self._default
        finally:
            self._pending.pop(rid, None)

    def resolve(self, request_id: str | None, approved: bool) -> bool:
        fut = self._pending.get(request_id) if request_id else next(iter(self._pending.values()), None)
        if fut is None or fut.done():
            return False
        fut.set_result(bool(approved))
        return True


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

    包住 AgentSession（P3）；in-process。final_response 从 agent 的 emit 流派生
    （docs/17 Phase 0：agent.final_text() 累计 AssistantDelta.text，取代旧 capturing sink）。
    """

    # push 事件流的 ring buffer 上限（防膨胀，docs/16 #4）：超出丢最旧，events() 是近期快照。
    EVENT_LOG_MAX = 512

    def __init__(self, runtime: "AgentRuntime", agent, session: AgentSession,
                 *, lease=None, services: RuntimeServices | None = None) -> None:
        self._runtime = runtime
        self.agent = agent
        self.session = session
        self.services = services
        # docs/16 #4（EVENT-P2）：typed AgentEvent push 流。tap 挂在 agent.emit 的订阅者扇出腿上，
        # 每条事件包成 {thread_id, session_id, seq, type, event} 信封（绝不携带 tree entry id，
        # docs/12 boundary 5）——ring buffer 留快照（events()），listeners 实时收推送。
        self._seq = 0
        self._event_log: deque = deque(maxlen=self.EVENT_LOG_MAX)
        self._listeners: list = []
        self._agent_tap = self._on_agent_event
        agent._event_subscribers.append(self._agent_tap)
        # docs/14 SessionLease：active thread 持有这把会话写者租约（lock=True 的 SessionManager）。
        # rebind 把旧 lease 的底层 mgr close 掉、新 thread 持新 lease；真正 teardown（REPL 退出 /
        # 一次性结束）经 release_lease() 释放。dispose() **不**碰 lease（见下）。
        self._lease = lease
        self._disposed = False
        # thread 身份在构造时**快照** agent.session_id：in-place rebind（docs/14 P2）会原地改
        # agent.session_id，若此处用 live property，old/new thread（复用同一 Agent）的 thread_id
        # 会同时变成 new sid，dispose(old) 误注销 new。快照保证 thread 身份稳定、注册/注销不串。
        self._thread_id = agent.session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    # ── typed 事件 push（docs/16 #4）────────────────────────────────────────────
    def _envelope(self, type_: str, event) -> dict:
        self._seq += 1
        return serialize_event_envelope({
            "thread_id": self.thread_id,
            "session_id": self.agent.session_id,
            "seq": self._seq,
            "type": type_,
            "event": event,
        })

    def _push(self, env: dict) -> None:
        self._event_log.append(env)
        for fn in list(self._listeners):
            try:
                fn(env)
            except Exception:
                pass   # fire-and-forget：单个订阅者异常不影响其余订阅者与 turn

    def _on_agent_event(self, event) -> None:
        """agent.emit 扇出腿：typed AgentEvent → 信封 → ring buffer + listeners。"""
        self._push(self._envelope(getattr(event, "kind", type(event).__name__), event))

    def push_boundary(self, type_: str, **fields) -> None:
        """非 turn 事件的流边界（如 rebind 的 session_switch）：event 槽是 plain dict。"""
        self._push(self._envelope(type_, dict(fields)))

    def subscribe(self, listener) -> "Callable[[], None]":
        """订阅本 thread 的事件信封流；返回 unsubscribe 句柄（幂等）。"""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass
        return _unsubscribe

    @property
    def is_processing(self) -> bool:
        return self.agent.is_processing

    async def run(self, prompt: str) -> TurnResult:
        if self._disposed:
            # 被 dispose 的 thread 复用同一 Agent，但 Agent 可能已 rebind 到别的 session；
            # 让 stale 句柄驱动会写错 session（codex B2）。inert 化：拒绝运行。
            raise RuntimeError("RuntimeThread is disposed; obtain the current thread from the host")
        # docs/17 Phase 0：final_response 从 agent 的 emit 流派生（AssistantMessageCompleted），
        # 每 turn 入口重置累加器，取代旧 BufferSink/TeeSink 捕获。
        self.agent.reset_final_text()
        prev_in = self.agent.total_input_tokens
        prev_out = self.agent.total_output_tokens
        await self.session.run_turn(prompt)
        # 取消语义：chat() 把取消吞成 _aborted 并正常返回——必须在此 await 之后读 _aborted。
        status: "Literal['completed','cancelled']" = "cancelled" if self.agent._aborted else "completed"
        final = self.agent.final_text()
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
        """从 runtime registry 注销本 thread、置 disposed（使 run/cancel inert）（docs/14 P1）。

        **不** finalize tracer / 不动 Agent —— 那是 Agent.rebind_session（P2）的职责。in-place
        rebind 下 old/new thread 复用同一 Agent，dispose 只做 wrapper 级清理；unregister 按
        **对象身份** compare-and-delete，即便 old/new 共享同一 session_id 也不会误删 new。"""
        if self._disposed:
            return
        self.push_boundary("thread_invalidated", reason="dispose")
        self._disposed = True
        self._runtime.unregister(self)
        # 摘除 emit 订阅 tap（old/new thread 复用同一 Agent：disposed thread 不再累积事件）。
        try:
            self.agent._event_subscribers.remove(self._agent_tap)
        except ValueError:
            pass

    def release_lease(self) -> None:
        """释放本 thread 持有的会话写锁（幂等）。真正 teardown 时调用：REPL 退出、一次性模式结束。

        **与 dispose 区分**：dispose 是 rebind 时对**旧 wrapper** 的清理，此时 lease 的底层 mgr 已被
        `Agent.rebind_session` close（或转交新 thread），旧 thread 绝不能再 close 一次 active 写锁；
        故 dispose 不碰 lease。release_lease 只在 host 退出当前 active thread（无后继）时由调用方显式调。"""
        if self._lease is not None:
            try:
                self._lease.close()
            except Exception:
                pass
            self._lease = None

    def tokens(self) -> dict:
        return self.agent.get_token_usage()

    def status(self) -> dict:
        """会话状态快照——供客户端（footer / RPC / 状态栏）读取，不再跨边界 reach 进 Agent 私有面
        （docs/17 Phase 5a）。高频可调（footer 每次重绘）；纯读、无副作用。"""
        import os as _os
        a = self.agent
        mgr = getattr(a, "_session_mgr", None)
        mode = getattr(a, "_thinking_mode", "disabled")
        return {
            "session_id": a.session_id,
            "cwd": self.services.cwd if self.services is not None else (mgr._cwd() if mgr is not None else _os.getcwd()),
            "session_name": (mgr.name() if mgr is not None else None),
            "input_tokens": a.total_input_tokens,
            "output_tokens": a.total_output_tokens,
            "context_used": getattr(a, "last_input_token_count", 0),
            "cost_usd": a._get_current_cost_usd(),
            "context_window": getattr(a, "effective_window", 0),
            "model": a.model,
            "thinking": None if mode == "disabled" else mode,
        }

    # ─── docs/19：sandbox profile/policy（public runtime API；不暴露 adapter argv）────────

    def sandbox_status(self) -> dict:
        """当前 sandbox 策略快照（profile + engine + fs/network + 后端可用性）。纯读。"""
        a = self.agent
        policy = a.sandbox_policy()
        sb = a._sandbox
        return {
            "profile": getattr(a, "_sandbox_profile", "default"),
            "engine": policy.engine.value,
            "network": policy.network.mode.value,
            "writable_roots": [str(p) for p in policy.filesystem.writable_roots],
            "protected_roots": [str(p) for p in policy.filesystem.protected_roots],
            "native_available": sb.native_available(),
            "vm_available": sb.vm_available(),
        }

    def set_sandbox_profile(self, name: str) -> str:
        """切换当前 session 的 sandbox profile（写入 runtime/agent state，非 module global）。

        非法 profile → 抛 ValueError（调用方渲染错误）。返回设定后的 profile 名。
        """
        from ..capabilities.sandbox import PROFILES
        if name not in PROFILES:
            raise ValueError(f"unknown profile: {name} (valid: {', '.join(PROFILES)})")
        self.agent._sandbox_profile = name
        return name

    def messages(self) -> list:
        """当前 active branch 的中立 Message[] 快照（docs/17 #2：从 canonical 树 build_context 派生）。

        重绘 / RPC get_state 的视图地基——**on-demand 重建**（每次重读树），非每帧热路径；无会话写者
        租约（_session_mgr=None）或树不可折叠时返回 []。中立 Message dict 与 provider 无关，
        客户端据此自渲染。"""
        mgr = getattr(self.agent, "_session_mgr", None)
        if mgr is None:
            return []
        try:
            return list(mgr.build_context().messages)
        except Exception:
            return []

    def transcript_messages(self) -> list:
        """当前 active branch 的真实对话消息，仅含 persisted MESSAGE entries。

        与 messages() 不同，这里故意不包含 custom context、compaction synthetic
        messages、repo-map volatile tail 等模型上下文材料；它是给 TUI/RPC 展示用户可见
        transcript 用的。
        """
        mgr = getattr(self.agent, "_session_mgr", None)
        if mgr is None:
            return []
        try:
            from ..session import tree as _tree
            out = []
            for e in mgr.get_branch():
                if e.type == _tree.MESSAGE:
                    msg = (e.data or {}).get("message")
                    if isinstance(msg, dict):
                        out.append(msg)
            return out
        except Exception:
            return []

    def state(self) -> dict:
        """完整会话快照（Pi `get_state` 对位）：status 字段 + is_processing + 中立 messages。
        供客户端重绘与 RPC get_state；on-demand 派生（含 messages 重建，勿当每帧热路径）。"""
        snap = self.status()
        snap["is_processing"] = self.is_processing
        snap["messages"] = self.messages()
        snap["transcript_messages"] = self.transcript_messages()
        return snap

    def session_stats(self) -> dict:
        mgr = getattr(self.agent, "_session_mgr", None)
        entries = mgr.entries() if mgr is not None else []
        messages = [e for e in entries if getattr(e, "type", None) == "message"]
        user_messages = 0
        assistant_messages = 0
        tool_results = 0
        for e in messages:
            msg = (e.data or {}).get("message") or {}
            role = msg.get("role")
            if role == "user":
                user_messages += 1
            elif role == "assistant":
                assistant_messages += 1
            elif role == "toolResult":
                tool_results += 1
        return {
            "session_id": self.session_id,
            "cwd": self.status()["cwd"],
            "session_name": self.session_name(),
            "message_count": len(messages),
            "user_messages": user_messages,
            "assistant_messages": assistant_messages,
            "tool_results": tool_results,
            "input_tokens": self.agent.total_input_tokens,
            "output_tokens": self.agent.total_output_tokens,
            "cost_usd": self.agent._get_current_cost_usd(),
        }

    def events(self) -> list[dict]:
        """push 流的近期快照（docs/16 #4：ring buffer，最多 EVENT_LOG_MAX 条）。

        每条是 {thread_id, session_id, seq, type, event} 信封；event 已在 runtime 边界转为
        JSON-able dict。实时消费用 subscribe(listener)。"""
        return list(self._event_log)

    # ── 命令面稳定 API（docs/17 B-list）────────────────────────────────────────
    # slash 命令经 CommandContext.thread 调这些方法，不再 reach 进 Agent 私有面
    # （_session_mgr / _spawn_* / _background_tasks / task_manager / agent_session）。
    # 这是面向 client（REPL / RPC / 其它）的命令操作面；导航类（new/resume/fork/clone）走
    # AgentRuntime + Control，不在此。

    @property
    def session_id(self) -> str:
        return self.agent.session_id

    @property
    def effective_window(self) -> int:
        return getattr(self.agent, "effective_window", 200000)

    @property
    def model(self) -> str:
        return getattr(self.agent, "model", "")

    @property
    def is_sub_agent(self) -> bool:
        return getattr(self.agent, "is_sub_agent", False)

    def clear_history(self) -> None:
        self.agent.agent_session.clear_history()

    async def compact(self, instructions: str | None = None) -> None:
        if instructions:
            await self.agent.agent_session.compact(instructions)
        else:
            await self.agent.agent_session.compact()

    def toggle_plan_mode(self) -> str:
        return self.agent.toggle_plan_mode()

    def show_cost(self) -> None:
        self.agent.show_cost()

    def move_to(self, entry_id: str | None):
        """in-file 树导航（移 active leaf）；返回重载后的 messages。"""
        return self.session.move_to(entry_id)

    def branch_summary_available(self, entry_id: str | None) -> bool:
        return self.session.branch_summary_available(entry_id)

    async def move_to_with_branch_summary(self, entry_id: str | None, *,
                                          focus: str | None = None):
        return await self.session.move_to_with_branch_summary(entry_id, focus=focus)

    def child_session_id(self, name: str) -> "str | None":
        fn = getattr(self.agent, "child_session_id", None)
        return fn(name) if callable(fn) else None

    def readonly_session(self):
        """Read-only session tree view for command handlers.

        Writes and lifecycle operations stay on RuntimeThread/AgentRuntime; the
        returned object intentionally does not expose SessionManager mutation APIs.
        """
        mgr = getattr(self.agent, "_session_mgr", None)
        return ReadOnlySessionView(mgr) if mgr is not None else None

    def session_name(self) -> str | None:
        mgr = getattr(self.agent, "_session_mgr", None)
        return mgr.name() if mgr is not None else None

    def set_session_name(self, name: str) -> None:
        mgr = getattr(self.agent, "_session_mgr", None)
        if mgr is None:
            raise RuntimeError("No active session writer lease for this session.")
        mgr.append_session_info(name)
        self.push_boundary("session_info_changed", name=mgr.name())

    def set_entry_label(self, entry_id: str, label: str) -> None:
        mgr = getattr(self.agent, "_session_mgr", None)
        if mgr is None:
            raise RuntimeError("No active session writer lease for this session.")
        mgr.append_label(entry_id, label)

    def can_switch(self) -> "tuple[bool, str | None]":
        if self.is_processing:
            return False, "a turn is currently running"
        tasks = getattr(self.agent, "_background_tasks", set())
        if tasks:
            return False, f"{len(tasks)} background task(s) still running"
        return True, None

    def task_list(self, status=None, kind=None) -> str:
        from ..tools.tasks_tool import list_tasks_text
        return list_tasks_text(self.agent.task_manager, status, kind)

    def task_output(self, task_id: str, tail_bytes: int = 8000) -> str:
        from ..tools.tasks_tool import task_output_text
        return task_output_text(self.agent.task_manager, task_id, tail_bytes)

    async def task_stop(self, task_id: str) -> str:
        from ..tools.tasks_tool import task_stop
        return await task_stop(self.agent.task_manager, self.agent._background_tasks, task_id)

    def agents_overview(self) -> str:
        from ..tools.tasks_tool import agents_overview_text
        return agents_overview_text(self._subagent_records())

    def agent_definitions(self) -> str:
        from ..tools.tasks_tool import list_agent_definitions_text
        return list_agent_definitions_text()

    def subagents(self) -> str:
        from ..tools.tasks_tool import list_subagents_text
        return list_subagents_text(self._subagent_records())

    def agent_detail(self, name: str) -> str:
        from ..tools.tasks_tool import agent_definition_detail_text, subagent_detail_text
        detail = agent_definition_detail_text(name)
        if detail is not None:
            return detail
        try:
            record = self.agent._reconcile_run(name)
        except Exception:
            record = None
        return subagent_detail_text(record)

    def _subagent_records(self):
        return self.agent._run_runtime.list(
            self.session_id,
            live_run_ids=self.agent._live_run_ids(),
        )

    async def execute_user_shell(self, command: str, *, timeout_ms: int = 120000,
                                 exclude_from_context: bool = True) -> str:
        """Run an explicit user shell command through the runtime audit boundary.

        This is not a model tool call and does not use tool permission approval,
        but it emits runtime events and uses the same structured shell runner.
        """
        from ..capabilities.sandbox import exec_host_command
        cwd = self.services.cwd if self.services is not None else self.status()["cwd"]
        self.push_boundary("user_shell_started", command=command,
                           exclude_from_context=exclude_from_context, cwd=cwd)
        r = await asyncio.to_thread(exec_host_command, command,
                                    cwd=cwd, timeout_ms=timeout_ms)
        self.push_boundary("user_shell_completed", command=command,
                           timed_out=r.get("timed_out"),
                           exit_code=r.get("exit_code"),
                           error=r.get("error"),
                           stdout_chars=len(r.get("stdout") or ""),
                           stderr_chars=len(r.get("stderr") or ""),
                           exclude_from_context=exclude_from_context, cwd=cwd)
        if r["timed_out"]:
            return f"$ {command}\n(timed out)"
        if r["error"] is not None:
            return f"$ {command}\nerror: {r['error']}"
        out = (r["stdout"] or "").rstrip()
        err = (r["stderr"] or "").rstrip()
        parts = [f"$ {command}"]
        if out:
            parts.append(out)
        if err:
            parts.append(err)
        if r["exit_code"] not in (0, None):
            parts.append(f"(exit {r['exit_code']})")
        return "\n".join(parts)

    def invoke_skill(self, name: str, args: str) -> SkillInvocation:
        """Resolve a user-invoked skill and return the prompt to run, if any."""
        from ..skills import execute_skill, get_skill_by_name, resolve_skill_prompt
        skill = get_skill_by_name(name)
        if not skill or not skill.user_invocable:
            return SkillInvocation(handled=False)
        if getattr(skill, "hooks", None):
            self.agent._register_skill_hooks(skill)
        if skill.context == "fork":
            result = execute_skill(skill.name, args)
            if not result:
                return SkillInvocation(handled=True, error=f"Unknown skill: {skill.name}")
            return SkillInvocation(
                handled=True,
                notice=f"Invoking skill: {skill.name}",
                prompt=f'Use the skill tool to invoke "{skill.name}" with args: {args or "(none)"}',
            )
        return SkillInvocation(
            handled=True,
            notice=f"Invoking skill: {skill.name}",
            prompt=resolve_skill_prompt(skill, args),
        )

    async def spawn_memory_consolidate(self) -> str:
        return await self.agent._spawn_memory_consolidate()

    async def spawn_memory_eval(self) -> str:
        return await self.agent._spawn_memory_eval()

    async def spawn_memory_optimize(self) -> str:
        return await self.agent._spawn_memory_optimize()


# ─── Runtime ────────────────────────────────────────────────

class AgentRuntime:
    """Codex 化 in-process facade：管 thread 生命周期。

    本步只对接已构造的 Agent（CLI 仍负责 Agent 构造/配置）；thread_start/resume 把它包成
    RuntimeThread。协议 / server 留待后续。

    会话导航语义表（pi 对齐，**唯一权威**——改任何一条须同步 commands/builtin.py 与 README）：

        /tree      同文件移动 leaf（AgentSession.move_to），不新建 session
        /fork      选 user message，复制其 parent 之前的路径到新 session（thread_fork，
                   header 记 parentSession+forkedBeforeEntryId），prompt 回填编辑器
        /clone     复制当前 leaf 所在 active branch 到新 session（thread_clone），编辑器为空
        /new       新顶层 session（thread_new），不带 parentSession
    """

    def __init__(self) -> None:
        self._threads: dict[str, RuntimeThread] = {}
        self._config: AgentConfig | None = None

    def _attach_agent(self, agent, *, approvals: "ApprovalManager | None" = None,
                      lease=None, services: RuntimeServices | None = None) -> RuntimeThread:
        """把 runtime 构造出的 Agent 纳管为 RuntimeThread。

        TurnResult.final_response 从 agent 的 emit 流派生（docs/17 Phase 0：agent.final_text()），
        无需外挂 capturing sink。

        lease（docs/14 SessionLease）：thread_start / replacement 已激活的会话写者租约——其
        `lease.manager` 注入给 agent 作 _session_mgr。本方法不自己取锁/建树。"""
        if approvals is not None:
            approvals.attach(agent)
        if lease is not None:
            agent._session_mgr = lease.manager
        if services is None:
            config = self._config or AgentConfig(
                permission_mode=getattr(agent, "_base_permission_mode", getattr(agent, "permission_mode", "default")),
                model=getattr(agent, "model", "claude-opus-4-6"),
                thinking=getattr(agent, "thinking", False),
                max_cost_usd=getattr(agent, "max_cost_usd", None),
                max_turns=getattr(agent, "max_turns", None),
                workspace_trusted=getattr(agent, "workspace_trusted", True),
                memory_backend=getattr(agent, "_memory_backend", None),
                session_id=getattr(agent, "session_id", None),
            )
            cwd = lease.manager._cwd() if lease is not None else os.getcwd()
            services = RuntimeServices.create(config, cwd=cwd)
        _apply_runtime_services(agent, services)
        session = AgentSession(agent)
        thread = RuntimeThread(self, agent, session, lease=lease, services=services)
        return self.register(thread)

    def thread_start(self, config: "AgentConfig", *, approvals: "ApprovalManager | None" = None,
                     lease=None, validate_session: bool = True) -> RuntimeThread:
        """从 AgentConfig 构造一个全新 Agent 并注册为首个 thread（docs/14 §3.1）。

        CLI / SDK / AppServer 的统一入口：config.build_agent() 造 Agent，再 attach（接线审批 +
        注入 lease + 注册）。live 切换（/new /resume…）走 thread_new/thread_resume
        （in-place rebind），不经此路径。"""
        self._config = config
        if lease is None:
            from ..session.lease import SessionLease
            lease = SessionLease.open_or_create(config.session_id or uuid.uuid4().hex[:8],
                                                cwd=config.cwd)
        services = RuntimeServices.create(config, cwd=lease.manager._cwd())
        effective_config = replace(config, session_id=lease.manager.session_id, cwd=services.cwd,
                                   memory_backend=services.memory_backend,
                                   workspace_trusted=services.workspace_trusted)
        self._config = effective_config
        with _push_cwd(services.cwd):
            agent = effective_config.build_agent()
        if validate_session:
            try:
                lease.manager.build_context()
            except BaseException:
                lease.close()
                raise
        return self._attach_agent(agent, approvals=approvals, lease=lease, services=services)

    def register(self, thread: "RuntimeThread") -> RuntimeThread:
        """把 thread 纳入 registry（按 thread_id）。runtime 构造器（P2）/ host.replace_thread 共用入口。"""
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
                           parent_session: "dict | None" = None,
                           reason: str = "replace") -> RuntimeThread:
        """把 host 当前 thread 的 Agent 原地 rebind 到 new_sid，建新 AgentSession+RuntimeThread
        包**同一** agent，经 host.replace_thread 切入（register 新 + dispose 旧）。返回新 thread。

        docs/14 SessionLease：在此**一处**完成 acquire-validate-new-before-release-old：
        ① `SessionLease.open_or_create(new_sid)` 取目标写锁（busy → SessionBusyError 上抛，
           不动旧 lease）；② `build_context()` 校验可折叠（torn/cyclic → 释放刚取的锁再上抛，
           避免泄漏/自锁死）；③ `rebind_session(lease.manager)` finalize 旧（close 旧锁）+ 装载新；
        ④ 新 RuntimeThread 持新 lease。调用方负责先过 host.can_switch() fail-closed 闸。

        resume 到**当前** session = no-op：直接返回当前 thread，绝不对同一 sid 取第二把锁（自锁死）。"""
        agent = host.current_thread.agent
        if new_sid == agent.session_id:
            return host.current_thread
        from ..session.lease import SessionLease
        old_sid = agent.session_id
        current_services = getattr(host.current_thread, "services", None)
        target_cwd = current_services.cwd if current_services is not None else None
        lease = SessionLease.open_or_create(new_sid, parent_session=parent_session, cwd=target_cwd)
        try:
            lease.manager.build_context()       # 校验目标树可折叠（在 finalize 旧 session 之前）
            config = self._config or AgentConfig(
                permission_mode=getattr(agent, "_base_permission_mode", getattr(agent, "permission_mode", "default")),
                model=getattr(agent, "model", "claude-opus-4-6"),
                thinking=getattr(agent, "thinking", False),
                max_cost_usd=getattr(agent, "max_cost_usd", None),
                max_turns=getattr(agent, "max_turns", None),
                workspace_trusted=getattr(agent, "workspace_trusted", True),
                memory_backend=getattr(agent, "_memory_backend", None),
                session_id=new_sid,
            )
            services = RuntimeServices.create(
                replace(config, session_id=new_sid, cwd=lease.manager._cwd()),
                cwd=lease.manager._cwd(),
            )
            host.current_thread.push_boundary("session_shutdown",
                                              reason=reason, target_session=new_sid)
            agent.rebind_session(lease.manager)  # finalize 旧（close 旧锁）+ rebuild 新
            _apply_runtime_services(agent, services)
            self._config = replace(config, session_id=new_sid, cwd=services.cwd,
                                   memory_backend=services.memory_backend,
                                   workspace_trusted=services.workspace_trusted)
            # rebind 边界（docs/16 #4）：旧 thread 的订阅者得知流被切走，新 thread 的流以切换开篇。
            host.current_thread.push_boundary("session_switch",
                                              from_session=old_sid, to_session=new_sid)
            new_thread = RuntimeThread(self, agent, AgentSession(agent), lease=lease, services=services)
            new_thread.push_boundary("session_switch", from_session=old_sid, to_session=new_sid)
            host.replace_thread(new_thread)
        except BaseException:
            # 整个切换都 fail-closed：build_context / rebind rebuild / 包 thread 任一步抛错，都释放刚取的
            # 新锁，避免新 lease 的 fd 泄漏（review medium：rebind 在 old_mgr.close() 后仍有 render/prompt
            # 重建步骤可能抛，那时新锁只由局部变量 lease 持有、不 close 就泄漏 + host 停在旧 closed thread）。
            lease.close()
            raise
        return new_thread

    @staticmethod
    def _mint_session_id() -> str:
        """铸造未占用的新 session id（8 hex，与主 agent 同格式）。re-mint 直到不与磁盘上已有
        session 撞——否则 rebind 会 open 到陌生 session 而非建空的（docs/14 P2 review）。"""
        from ..session.manager import SessionManager
        sid = uuid.uuid4().hex[:8]
        while SessionManager.exists(sid):
            sid = uuid.uuid4().hex[:8]
        return sid

    def thread_new(self, host) -> RuntimeThread:
        """新建空**顶层** canonical session 并切入（/new；语义表：不带 parentSession）。"""
        return self._switch_via_rebind(host, self._mint_session_id(), reason="new")

    def thread_resume(self, host, session_id: str) -> "RuntimeThread | None":
        """切到一个已存在的 session（/resume <id>）。docs/14 SessionLease：canonical 树是唯一权威——
        树缺 → 返回 None（canonical 树是唯一权威；legacy 导入面已删，docs/16 C-3）。
        目标被占用 → `_switch_via_rebind` 抛 SessionBusyError（调用方提示 `--fork`）。"""
        from ..session.manager import SessionManager
        if not SessionManager.exists(session_id):
            return None
        return self._switch_via_rebind(host, session_id, reason="resume")

    def startup_fork_session(self, source_sid: str) -> tuple[str | None, str | None]:
        """Clone a source session for startup ``--fork`` before the main thread exists.

        This keeps startup lifecycle work inside the runtime layer. The returned
        child session is intentionally unlocked; ``thread_start`` acquires the
        writer lease for the adopted child.
        """
        from ..session import tree as _tree
        from ..session.manager import SessionManager

        try:
            src = SessionManager.open(source_sid)
        except Exception as e:
            return None, f"could not open source session '{source_sid}': {e}"
        try:
            child = src.clone()
            return child.session_id, None
        except _tree.SessionTreeError as e:
            if "nothing to clone" not in str(e):
                return None, str(e)
            child = SessionManager.create(
                cwd=src._cwd(),
                parent_session={"sessionId": source_sid, "entryId": src.get_leaf()},
                lock=False,
            )
            return child.session_id, None
        except Exception as e:
            return None, f"could not fork session '{source_sid}': {e}"

    def thread_fork(self, host, source_sid: str, user_entry_id: str) -> "RuntimeThread | None":
        """pi /fork：复制 source 到选中 **user 消息**之前（其 parent 的 path-to-root）的新 session
        并切入；选中 prompt 由调用方预填编辑器。新 session header 一律记 parentSession 血缘
        （含空前缀情形——children(source)/parent 导航与 trajectory 审计依赖它，review P1）。

        fail-closed（review P2）：本方法是 runtime facade（SDK/AppServer 可直调），user-message
        校验在此强制——非 user MESSAGE entry（assistant/toolResult/compaction…）→ None；
        CLI handler 的同类校验只是更友好的 UX 提示。源无树 / entry 不存在 / 复制失败 → None。"""
        from ..session import tree as _tree
        from ..session.manager import SessionManager
        if not SessionManager.exists(source_sid):
            return None
        src = SessionManager.open(source_sid)
        sel = next((e for e in src.entries() if e.id == user_entry_id), None)
        if (sel is None or sel.type != _tree.MESSAGE
                or (sel.data.get("message") or {}).get("role") != "user"):
            return None
        cut = sel.parentId
        # 选中消息之前无可复制内容（parent=None，或 pre-3a 树里 parent 是 session_start header）
        # → 新建**空** session，但血缘照记（不可复用裸 thread_new：会丢 parentSession）。
        if cut is None or all(e.type == _tree.SESSION_START for e in src.get_branch(cut)):
            return self._switch_via_rebind(
                host, self._mint_session_id(),
                parent_session={"sessionId": source_sid, "entryId": cut,
                                "forkedBeforeEntryId": user_entry_id},
                reason="fork")
        try:
            child = src.clone(cut, parent_session_extra={"forkedBeforeEntryId": user_entry_id})
        except Exception:
            return None
        return self._switch_via_rebind(host, child.session_id, reason="fork")

    def thread_clone(self, host, source_sid: str, entry_id: "str | None" = None) -> "RuntimeThread | None":
        """跨文件 clone：复制 source 的 path-to-root（entry_id 缺省 = 当前 leaf）到新 session
        （header 记 parentSession 血缘）并切入。源无 canonical 树 / clone 失败 → None。

        pi 命令语义（ac3e78e）：/clone 固定当前 leaf、无参；entry_id 参数保留给内部调用方
        （/resume --fork 整 branch fork 等）。「在某条 user 消息之前分叉」是 thread_fork 的职责。"""
        from ..session.manager import SessionManager
        if not SessionManager.exists(source_sid):
            return None
        try:
            child = SessionManager.open(source_sid).clone(entry_id)
        except Exception:
            return None
        return self._switch_via_rebind(host, child.session_id, reason="fork")
