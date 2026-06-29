"""extensions/host.py — ExtensionHost: registry + lifecycle dispatcher (docs/22 §7 Phase 0).

The host is not a business module: it loads built-in system manifests, runs each
extension's `activate(api)` to collect contributions, and binds runtime services
so handlers can be invoked with a fresh call-time context. It mirrors Pi's
`ExtensionRunner`: registries are populated at activation; host actions
(run_task / emit / create_context) only work after `bind_runtime`.

Lifecycle:
    load_system_extensions()  -> host with manifests (no activation yet)
    activate_all()            -> run activate(api); collect contributions
    bind_runtime(thread, svc) -> host actions become live (is_active=True)
    invalidate(reason)        -> is_active=False; contexts created from it go stale

A fresh host is built per `RuntimeServices` (i.e. per session/cwd). On a session
switch the old thread's host is invalidated and the new services build a new one,
so a captured context never writes the wrong session (docs/22 §5.0.1 / §9.1.6).
"""
from __future__ import annotations

import importlib

from .api import ExtensionAPI
from .context import (
    ApprovalInboxView, EventSink, ExtensionCommandContext, ExtensionContext,
    ExtensionModelRouter, SpawnCap, TaskManagerView, WorkspaceProviderView,
)
from .errors import ExtensionLoadError, ExtensionRuntimeError
from .manifest import ExtensionManifest
from .registry import ContributionRegistry


def _system_manifests() -> list[ExtensionManifest]:
    """Built-in system extension manifests.

    No project/user/`.nanocode/extensions` discovery (docs/22 §1.2 reject #1)."""
    from .memory_evolution.manifest import MANIFEST as MEMORY_EVOLUTION_MANIFEST
    from .orchestration.manifest import MANIFEST as ORCHESTRATION_MANIFEST
    return [MEMORY_EVOLUTION_MANIFEST, ORCHESTRATION_MANIFEST]


def _resolve_entrypoint(entrypoint: str):
    module_name, _, attr = entrypoint.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as e:  # noqa: BLE001 — surface as a load error, fail loud
        raise ExtensionLoadError(f"cannot import extension module {module_name!r}: {e}") from e
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise ExtensionLoadError(
            f"extension entrypoint {entrypoint!r} does not resolve to a callable")
    return fn


def _make_ext_adapter(handler, tool_name: str):
    """Build a `Tool.run(ctx, inp)` adapter that invokes an extension tool handler
    with the sealed ToolContext (docs/24 Phase 4b).

    The handler is called `handler(inp, ctx)`; coroutine results are awaited.
    The adapter is always async (returns a coroutine) so the dispatch chokepoint's
    `_maybe_await` awaits it uniformly. Handler exceptions are coerced to an error
    string — an extension tool must never crash the host dispatch loop."""
    import inspect

    async def _run(ctx, inp):
        try:
            result = handler(inp, ctx)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:  # noqa: BLE001 — isolate extension faults
            return f"Error: extension tool {tool_name!r} failed: {e}"
        return result if isinstance(result, str) else str(result)

    return _run


class ExtensionHost:
    def __init__(self, manifests: list[ExtensionManifest]) -> None:
        self.manifests = list(manifests)
        self.registry = ContributionRegistry()
        self._activated = False
        self._active = False          # True only between bind_runtime and invalidate
        self._thread = None
        self._services = None

    # ── load / activate ───────────────────────────────────────────────
    @classmethod
    def load_system_extensions(cls) -> "ExtensionHost":
        return cls(_system_manifests())

    def activate_all(self) -> "ExtensionHost":
        """Run each extension's activate(api). Registration-only — no host access,
        no env/project reads (the hidden-agent-vs-custom-agent conflict check is
        deferred to bind_runtime, a host/trust phase). Conflict rules in the
        ContributionRegistry that are pure (dup command/task/hidden-agent) fire
        here (fail loud)."""
        if self._activated:
            return self
        for manifest in self.manifests:
            activate = _resolve_entrypoint(manifest.entrypoint)
            api = ExtensionAPI(self.registry, extension_id=manifest.id)
            try:
                activate(api)
            except ExtensionLoadError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ExtensionLoadError(
                    f"extension {manifest.id!r} activation failed: {e}") from e
        self._activated = True
        return self

    def _check_hidden_agent_conflicts(self) -> None:
        """Fail loud if a registered hidden agent collides with a custom (project/
        user) agent definition. Run at bind time (a host/trust phase) so activation
        stays free of env/project reads (docs/22 §7 Phase 0 conflict rule #3)."""
        if not self.registry.hidden_agents:
            return
        try:
            from ..agents.registry import discover_custom_agents
            custom_types = set(discover_custom_agents().keys())
        except Exception:
            return  # discovery is internally fail-closed; absence => no custom collision
        for name, (_profile, ext_id) in self.registry.hidden_agents.items():
            if name in custom_types:
                raise ExtensionLoadError(
                    f"extension {ext_id!r}: hidden agent {name!r} collides with a "
                    f"custom agent definition")

    # ── bind / invalidate ─────────────────────────────────────────────
    def bind_runtime(self, thread, services) -> None:
        if not self._activated:
            raise ExtensionRuntimeError("activate_all() must run before bind_runtime()")
        # Trust/runtime phase: now (not at activation) check hidden agents against
        # discovered custom agent definitions (fail loud on collision).
        self._check_hidden_agent_conflicts()
        self._thread = thread
        self._services = services
        self._active = True

    def invalidate(self, reason: str) -> None:
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    # ── command surface (static) ──────────────────────────────────────
    def command_contributions(self) -> list:
        """[(CommandContribution, ext_handler)] for the entrypoints bridge.

        Handlers are static; they resolve the *live* bound host from the command
        context at call time, so this list can be built once at module load from
        an activated-but-unbound host."""
        return [(rc.contribution, rc.handler) for rc in self.registry.commands.values()]

    # ── tool surface (docs/24 Phase 4b) ───────────────────────────────
    def tool_contributions(self) -> list:
        """Wrap each registered extension tool into a `tools.spec.Tool` for the
        agent's per-agent overlay registry.

        Each tool is namespaced `ext__<extension_id>__<name>`, source=EXT,
        trust=UNTRUSTED, needs=declared. The handler is wrapped in an adapter
        `run(ctx, inp)` that invokes it with the **sealed** ToolContext (UNTRUSTED
        ⟹ every capability slot None). The adapter awaits coroutine handlers and
        coerces the result to a string. Errors from the handler are caught and
        returned as an error string (never crash the dispatch chokepoint)."""
        from ..tools.spec import Tool
        from ..tools.types import ToolSource, Trust

        out: list = []
        for rt in self.registry.tools.values():
            prefixed = f"ext__{rt.extension_id}__{rt.name}"
            schema = dict(rt.schema)
            schema["name"] = prefixed
            out.append(Tool(
                schema=schema,
                run=_make_ext_adapter(rt.handler, prefixed),
                source=ToolSource.EXT,
                trust=Trust.UNTRUSTED,
                needs=rt.needs,
            ))
        return out

    # ── context factories (call-time) ─────────────────────────────────
    def _spawn_allowed_agent_types(self) -> "frozenset[str]":
        """docs/26 阶段1 ②：受信 spawn 槽可 spawn 的 agent_type 集。

        = 声明了 `spawn:reserved` capability 的扩展所贡献的 hidden agents（双绑定：
        opt-in capability × 该扩展自己注册、且内核已在 bind_runtime 校验过的 reserved/hidden
        agent）。空集 → ctx 不挂 spawn 槽（`ctx.spawn is None`）。"""
        from .manifest import SPAWN_RESERVED
        granted = {m.id for m in self.manifests if SPAWN_RESERVED in m.capabilities}
        if not granted:
            return frozenset()
        return frozenset(
            name for name, (_profile, ext_id) in self.registry.hidden_agents.items()
            if ext_id in granted)

    def _orchestrate_granted(self) -> bool:
        """docs/26 §0.6 阶段1：是否有 active 扩展声明了 `spawn:orchestrate`（解锁编排原语）。"""
        from .manifest import SPAWN_ORCHESTRATE
        return any(SPAWN_ORCHESTRATE in m.capabilities for m in self.manifests)

    def _build_context_fields(self) -> dict:
        if not self._active:
            raise ExtensionRuntimeError(
                "extension host is not bound to a live runtime (or was invalidated)")
        thread = self._thread
        services = self._services
        agent = getattr(thread, "_agent", None)
        memory = getattr(services, "memory_service", None) if services is not None else None
        session = thread.readonly_session() if thread is not None else None
        host_model = getattr(thread, "model", "") or ""
        allowed_spawn = self._spawn_allowed_agent_types()
        can_orchestrate = self._orchestrate_granted()
        return dict(
            host=self,
            cwd=(services.cwd if services is not None else ""),
            thread=thread,
            session=session,
            memory=memory,
            tasks=TaskManagerView(self, agent.task_manager) if agent is not None else None,
            models=ExtensionModelRouter(self, host_model=host_model,
                                        roles=dict(self.registry.model_roles)),
            events=EventSink(self, agent.emit) if agent is not None else None,
            spawn=(SpawnCap(self, thread, allowed_agent_types=allowed_spawn,
                            can_orchestrate=can_orchestrate)
                   if thread is not None and (allowed_spawn or can_orchestrate) else None),
            approvals=(ApprovalInboxView(self, thread)
                       if thread is not None and can_orchestrate else None),
            workspace=(WorkspaceProviderView(self) if can_orchestrate else None),
        )

    def create_context(self) -> ExtensionContext:
        return ExtensionContext(**self._build_context_fields())

    def create_command_context(self) -> ExtensionCommandContext:
        fields = self._build_context_fields()
        thread = self._thread

        async def _wait_for_idle() -> None:
            return None  # REPL is strictly serial; idle by the time a command runs

        return ExtensionCommandContext(wait_for_idle=_wait_for_idle, **fields)

    # ── task dispatch ─────────────────────────────────────────────────
    async def run_task(self, kind: str, payload: dict, *, task_id: str) -> None:
        """Dispatch a task kind to its registered handler with a fresh context.

        A handler error is surfaced on the task record (the caller's detached
        runner sets failed status); it never crashes the host loop."""
        entry = self.registry.task_kinds.get(kind)
        if entry is None:
            raise ExtensionRuntimeError(f"no extension task handler for kind {kind!r}")
        handler, _ext = entry
        ctx = self.create_context()
        await handler(ctx, payload, task_id=task_id)

    # ── orchestration dispatch (docs/26 §0.6 阶段1) ───────────────────
    async def run_orchestrator(self, payload: dict) -> str:
        """Invoke the registered orchestration handler with a fresh spawn-capable
        context, returning its aggregated result string.

        Foreground (blocking) orchestration awaits this directly; background
        orchestration runs it inside a detached task (the caller tags the task with
        the group id). No orchestrator registered ⟹ fail loud — there is no in-kernel
        chain/parallel fallback (the policy lives only in the extension)."""
        if self.registry.orchestrator is None:
            raise ExtensionRuntimeError("no orchestration extension is registered")
        handler, _ext = self.registry.orchestrator
        ctx = self.create_context()
        return await handler(ctx, payload)

    # ── lifecycle dispatch ────────────────────────────────────────────
    async def emit(self, event: str, payload: dict | None = None) -> None:
        """Run lifecycle handlers for an event. Handler errors are isolated
        (surfaced as notices) — one bad handler never blocks the others."""
        handlers = self.registry.lifecycle_handlers.get(event, [])
        if not handlers:
            return
        ctx = self.create_context()
        for handler, ext_id in handlers:
            try:
                await handler(ctx, payload or {})
            except Exception as e:  # noqa: BLE001
                if ctx.events is not None:
                    ctx.events.notice(f"[extension {ext_id}] {event} handler failed: {e}",
                                      level="warn")
