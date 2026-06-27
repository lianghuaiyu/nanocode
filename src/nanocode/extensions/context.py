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

    与 UNTRUSTED 工具封印一致性：UNTRUSTED 只封印模型可调的扩展**工具** ctx 槽；本槽在
    context-view 侧（与 tasks/events/models 同类），由 host 仅向声明 capability 的扩展授予。"""

    def __init__(self, host, thread, *, allowed_agent_types: "frozenset[str]") -> None:
        self._host = host
        self._thread = thread
        self._allowed = frozenset(allowed_agent_types)

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
