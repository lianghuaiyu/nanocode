"""extensions/context.py — call-time ExtensionContext (docs/22 §7 Phase 0 / §5.0.1).

Mirrors Pi's `ExtensionContext` / `ExtensionCommandContext` split:

- the context is built by `ExtensionHost.create_context()` *per command / task /
  hook invocation* — extensions must not cache it across calls.
- a context bound to a host that has since been invalidated (session
  replacement / teardown) is **stale**: mutating capabilities fail loud
  (`ExtensionRuntimeError`) instead of writing the wrong session (docs/22 §9.1.6).
- the session view is read-only; the context never exposes the raw `Agent`,
  `_session_mgr`, or `_background_tasks`.

The command context additionally exposes `wait_for_idle` (stronger session
control surface, kept separate from plain event/task contexts).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from .errors import ExtensionRuntimeError


class _StaleGuard:
    """Mixin giving a capability view a fail-loud staleness check."""

    _host: object

    def _ensure_active(self) -> None:
        host = self._host
        if host is None or not getattr(host, "is_active", False):
            raise ExtensionRuntimeError(
                "extension context is stale (session was replaced or torn down); "
                "obtain a fresh context from the current ExtensionHost")


class EventSink(_StaleGuard):
    """Emit host-observable notices/diagnostics from an extension handler.

    Diagnostics go through the live agent event stream, not the session tree —
    extension state never pollutes LLM context (docs/22 §5.0.1)."""

    def __init__(self, host, emit: Callable[[object], None]) -> None:
        self._host = host
        self._emit = emit

    def notice(self, text: str, *, level: str = "info") -> None:
        self._ensure_active()
        from ..agent import events as _events
        try:
            self._emit(_events.NoticeRaised(text=text, level=level))
        except Exception:
            pass  # fire-and-forget: a dead sink never breaks the handler


class TaskManagerView(_StaleGuard):
    """Narrow, fail-loud view of the host TaskManager for extension tasks.

    Exposes only what task handlers need (read + status/result updates); it does
    not expose subagent spawning or the raw background-task set."""

    def __init__(self, host, task_manager) -> None:
        self._host = host
        self._tm = task_manager

    def get_task(self, task_id: str):
        self._ensure_active()
        return self._tm.get_task(task_id)

    def update_task(self, task_id: str, **fields):
        self._ensure_active()
        return self._tm.update_task(task_id, **fields)


class SpawnCap(_StaleGuard):
    """受信 spawn 槽（docs/26 阶段1 ②）：扩展可 spawn 的 narrow 能力。

    扩展只报 `agent_type`（限于本扩展贡献、且 manifest 声明了 `spawn:reserved` 的 reserved/
    hidden agent）；子 agent 的工具集 / sandbox 由**内核**派生（child_tools→effective_child_tools
    的 allow∩/deny∪/剔 agent + 父 sandbox 继承）。本视图**签名无 tools/sandbox 参数**——提权在
    类型层就不可能：扩展永远拿不到「给子裸配工具」的权力（docs/26 §0.3 O5 命门）。

    `can_orchestrate`（docs/26 §0.6 阶段1，capability `spawn:orchestrate`）额外解锁编排原语
    `run`/`run_background`/`new_group`/`cancel_group`——可 spawn 通用模型类型（general/coder/
    explore/plan/custom），但**拒 reserved**类型；子 caps 同样由内核派生（仍非提权）。未授予
    该 capability 时这些方法 fail loud。

    与 UNTRUSTED 工具封印一致性：UNTRUSTED 只封印模型可调的扩展**工具** ctx 槽；本槽在
    context-view 侧（与 tasks/events/models 同类），由 host 仅向声明 capability 的扩展授予。"""

    def __init__(self, host, thread, *, allowed_agent_types: "frozenset[str]",
                 can_orchestrate: bool = False) -> None:
        self._host = host
        self._thread = thread
        self._allowed = frozenset(allowed_agent_types)
        self._can_orchestrate = can_orchestrate

    async def reserved(self, agent_type: str, prompt: str, *,
                       model: "str | None" = None,
                       timeout_ms: "int | None" = None) -> str:
        self._ensure_active()
        if agent_type not in self._allowed:
            raise ExtensionRuntimeError(
                f"extension may not spawn agent_type {agent_type!r} "
                f"(granted reserved set: {sorted(self._allowed)})")
        return await self._thread.run_reserved_subagent(
            agent_type, prompt, model=model, timeout_ms=timeout_ms)

    # ── 编排原语（capability spawn:orchestrate；子 caps 仍内核派生，非提权）──────────
    # 三类成员原语逐一对应内核三原语（行为保真，docs/26 §0.6 阶段1）：
    #   run_fresh        → run_fresh_subagent        （前台,bounded envelope）
    #   run_step         → spawn_subagent            （后台 chain 步:await + group/inject）
    #   run_background   → spawn_background_subagent  （后台 parallel 成员:detached）
    # 外加 new_group / cancel_group / launch_coordinator（后台 chain 的 detached coordinator）。
    def _ensure_orchestrate(self) -> None:
        self._ensure_active()
        if not self._can_orchestrate:
            raise ExtensionRuntimeError(
                "extension was not granted the 'spawn:orchestrate' capability")

    @staticmethod
    def _reject_reserved(agent_type: str) -> None:
        """编排不得 spawn reserved/hidden agent（它们是内核/扩展私有的特权类型）。"""
        from ..agents.registry import RESERVED_AGENT_TYPES
        if agent_type in RESERVED_AGENT_TYPES:
            raise ExtensionRuntimeError(
                f"orchestration may not spawn reserved agent_type {agent_type!r}")

    def new_group(self) -> str:
        self._ensure_orchestrate()
        return self._thread.new_orchestration_group()

    async def run_fresh(self, agent_type: str, prompt: str, *, description: "str | None" = None,
                        timeout_ms: "int | None" = None, context_mode: str = "fresh",
                        isolation: "str | None" = None, parallel: bool = False) -> str:
        """前台编排成员：返回 bounded ResultEnvelope（供 {previous} 串接 / fan-in 聚合）。"""
        self._ensure_orchestrate()
        self._reject_reserved(agent_type)
        return await self._thread.run_orchestration_member(
            agent_type, prompt, description=description, timeout_ms=timeout_ms,
            context_mode=context_mode, isolation=isolation, parallel=parallel)

    async def run_step(self, agent_type: str, prompt: str, *, group_id: str,
                       description: "str | None" = None, inject_summary: bool = True,
                       result_summary: "str | None" = None, timeout_ms: "int | None" = None,
                       context_mode: str = "fresh") -> str:
        """后台 chain 步：await 子完成,返回原始 text（供 {previous}）；带 group/inject。"""
        self._ensure_orchestrate()
        self._reject_reserved(agent_type)
        return await self._thread.run_orchestration_step(
            agent_type, prompt, group_id=group_id, description=description,
            inject_summary=inject_summary, result_summary=result_summary,
            timeout_ms=timeout_ms, context_mode=context_mode)

    async def run_background(self, agent_type: str, prompt: str, *,
                             group_id: "str | None" = None, description: "str | None" = None,
                             inject_summary: bool = True,
                             result_summary: "str | None" = None,
                             timeout_ms: "int | None" = None, context_mode: str = "fresh",
                             isolation: "str | None" = None) -> str:
        """后台 parallel 成员：detached run，返回 run_id（完成摘要按 inject_summary PUSH 回父）。"""
        self._ensure_orchestrate()
        self._reject_reserved(agent_type)
        return await self._thread.spawn_orchestration_background(
            agent_type, prompt, group_id=group_id, description=description,
            inject_summary=inject_summary, result_summary=result_summary,
            timeout_ms=timeout_ms, context_mode=context_mode, isolation=isolation)

    def launch_coordinator(self, coro, *, group_id: str) -> None:
        """把扩展的后台 chain coordinator 协程登记为内核追踪的 detached task（tagged group_id
        供整组 cancel）。coordinator 自身不持 run_record（步骤才是 run）。"""
        self._ensure_orchestrate()
        self._thread.launch_orchestration_coordinator(coro, group_id=group_id)

    async def cancel_group(self, group_id: str) -> str:
        """级联取消整组（run_cancel 的 _nanocode_group_id 不动点扫描）。"""
        self._ensure_orchestrate()
        return await self._thread.cancel_runs(group_id)


class ExtensionModelRouter(_StaleGuard):
    """Resolve an extension model role to a concrete model id (docs/22 §5.4).

    Resolution order per role policy: host-only env override (if the policy names
    one and it is set) → policy default ("host" = the host's current model)."""

    def __init__(self, host, *, host_model: str, roles: dict) -> None:
        self._host = host
        self._host_model = host_model
        self._roles = roles  # role -> (ModelRolePolicy, extension_id)

    def resolve(self, role: str) -> str:
        self._ensure_active()
        import os
        entry = self._roles.get(role)
        if entry is None:
            raise ExtensionRuntimeError(f"unknown extension model role: {role!r}")
        policy, _ext = entry
        if policy.env_var:
            override = (os.environ.get(policy.env_var) or "").strip()
            if override:
                return override
        if policy.default == "host" or not policy.default:
            return self._host_model
        return policy.default


class ExtensionContext:
    """Call-time context handed to event/task handlers.

    Built fresh by `ExtensionHost.create_context()` per invocation. `thread`,
    `session`, and `memory` are exposed as stale-guarded properties: once the
    owning host is invalidated (session replacement / teardown), accessing them —
    like the `tasks`/`models`/`events` views — fails loud, so a cached context can
    never reach the raw RuntimeThread / MemoryService to write the wrong session
    (docs/22 §9.1.6). `session` is read-only; the raw Agent / `_session_mgr` /
    `_background_tasks` are never exposed."""

    def __init__(self, *, host, cwd: str, thread, session, memory,
                 tasks: "TaskManagerView", models: "ExtensionModelRouter",
                 events: "EventSink", spawn: "SpawnCap | None" = None,
                 signal=None) -> None:
        self._host = host
        self.cwd = cwd
        self._thread = thread
        self._session = session
        self._memory = memory
        self.tasks = tasks
        self.models = models
        self.events = events
        self.spawn = spawn
        self.signal = signal

    def _ensure_active(self) -> None:
        if self._host is None or not getattr(self._host, "is_active", False):
            raise ExtensionRuntimeError(
                "extension context is stale (session was replaced or torn down); "
                "obtain a fresh context from the current ExtensionHost")

    @property
    def thread(self):
        self._ensure_active()
        return self._thread

    @property
    def session(self):
        self._ensure_active()
        return self._session

    @property
    def memory(self):
        self._ensure_active()
        return self._memory


class ExtensionCommandContext(ExtensionContext):
    """Command context: stronger session-control surface than event/task ctx."""

    def __init__(self, *, wait_for_idle: "Callable[[], Awaitable[None]] | None" = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.wait_for_idle = wait_for_idle
