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
    ExtensionModelRouter, MemoryEvolutionCap, SpawnCap, TaskManagerView,
    WorkspaceProviderView,
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
        self._manifest_by_id = {m.id: m for m in self.manifests}
        self.registry = ContributionRegistry()
        self._activated = False
        self._active = False          # True only between bind_runtime and invalidate
        self._thread = None
        self._services = None

    # ── load / activate ───────────────────────────────────────────────
    @classmethod
    def load_system_extensions(cls) -> "ExtensionHost":
        """First-party system extensions only (no project/user discovery).

        Used at module load by the command bridge (no cwd, side-effect-free import
        contract). Per-session bootstrap uses `load_all_extensions(cwd=...)` instead,
        which additionally discovers untrusted project extensions (docs/26 G6)."""
        return cls(_system_manifests())

    @classmethod
    def load_all_extensions(cls, *, cwd: "str | None" = None) -> "ExtensionHost":
        """System extensions + discovered untrusted project extensions (docs/26 G6).

        Untrusted extensions are declarative-only (no Python entrypoint); their only
        execution path is an out-of-process MCP server the host spawns. Discovery is
        fail-soft per extension — one malformed manifest never breaks the session."""
        return cls(_system_manifests() + cls._discover_untrusted_manifests(cwd))

    @staticmethod
    def _discover_untrusted_manifests(cwd: "str | None" = None) -> list[ExtensionManifest]:
        """Scan `<cwd>/.nanocode/extensions/*/manifest.json` for untrusted extensions.

        Strict + fail-soft: each manifest must parse to a valid `kind="untrusted"`
        manifest (only `mcp_servers`; `ExtensionManifest.__post_init__` enforces the
        rest) or it is skipped. Missing dir → []. Project-level only (no user-level /
        versioning / enable-disable / hot-reload — docs/26 G6 defers those)."""
        import json
        from pathlib import Path
        from ..paths import project_config_dir
        from .manifest import ExtensionContributes, McpServerSpec

        root = project_config_dir(Path(cwd) if cwd else None) / "extensions"
        if not root.exists():
            return []
        out: list[ExtensionManifest] = []
        for d in sorted(root.iterdir()):
            mf = d / "manifest.json"
            if not mf.is_file():
                continue
            try:
                raw = json.loads(mf.read_text(encoding="utf-8"))
                servers = tuple(
                    McpServerSpec(
                        name=str(s["name"]), command=str(s["command"]),
                        args=tuple(str(a) for a in s.get("args", ())),
                        env=tuple((str(k), str(v)) for k, v in (s.get("env") or {}).items()))
                    for s in raw.get("mcp_servers", []))
                out.append(ExtensionManifest(
                    id=str(raw["id"]), kind="untrusted", entrypoint="",
                    contributes=ExtensionContributes(mcp_servers=servers)))
            except Exception:  # noqa: BLE001 — fail-soft per extension; never break the session
                continue
        return out

    def activate_all(self) -> "ExtensionHost":
        """Run each extension's activate(api). Registration-only — no host access,
        no env/project reads (the hidden-agent-vs-custom-agent conflict check is
        deferred to bind_runtime, a host/trust phase). Conflict rules in the
        ContributionRegistry that are pure (dup command/task/hidden-agent) fire
        here (fail loud).

        Untrusted extensions (docs/26 G6) are declarative-only — skipped here: they
        have no entrypoint and contribute no in-process code, only MCP server specs
        surfaced by `mcp_contributions()`."""
        if self._activated:
            return self
        for manifest in self.manifests:
            if manifest.kind == "untrusted":
                continue
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
        """[(CommandContribution, ext_handler, extension_id)] for the entrypoints bridge.

        Handlers are static; they resolve the *live* bound host from the command
        context at call time, so this list can be built once at module load from
        an activated-but-unbound host. `extension_id` is carried so the bridge can
        build a per-extension-scoped command context (docs/26 G6)."""
        return [(rc.contribution, rc.handler, rc.extension_id)
                for rc in self.registry.commands.values()]

    # ── MCP surface (declarative, docs/26 G6) ─────────────────────────
    def mcp_contributions(self) -> "dict[str, dict]":
        """Aggregate declared MCP servers across manifests, namespaced by extension.

        Returns the dict shape `McpManager` consumes: `{server_key: {command, args?,
        env?}}`. `server_key` is sanitized to contain NO `__` (MCP tool names are
        `mcp__<server>__<tool>` and `call_tool` splits on `__`, so a `__` inside the
        server segment would mis-route) — extension id + spec name joined with `-`.

        Independent of `activate_all()`: reads manifest data, so untrusted
        (never-activated) extensions contribute here too."""
        def _key(ext_id: str, name: str) -> str:
            return f"{ext_id}.{name}".replace("__", "-").replace(".", "-")

        out: "dict[str, dict]" = {}
        for m in self.manifests:
            for spec in m.contributes.mcp_servers:
                cfg: dict = {"command": spec.command}
                if spec.args:
                    cfg["args"] = list(spec.args)
                if spec.env:
                    cfg["env"] = dict(spec.env)
                out[_key(m.id, spec.name)] = cfg
        return out

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

    # ── context factories (call-time, docs/26 G6 per-extension scoped) ─
    def _spawn_allowed_agent_types(self, extension_id: str) -> "frozenset[str]":
        """docs/26 阶段1 ②：受信 spawn 槽可 spawn 的 agent_type 集（**按扩展作用域**）。

        = 该扩展若声明了 `spawn:reserved`，则为它**自己贡献**的 hidden agents；否则空集。
        空集 → ctx 不挂 spawn 槽（`ctx.spawn is None`）。每个 handler 只拿到它自己声明、
        自己贡献的 reserved agent（codex contributor 模式，docs/26 G6 Tier 2）。"""
        from .manifest import SPAWN_RESERVED
        m = self._manifest_by_id.get(extension_id)
        if m is None or SPAWN_RESERVED not in m.capabilities:
            return frozenset()
        return frozenset(
            name for name, (_profile, ext_id) in self.registry.hidden_agents.items()
            if ext_id == extension_id)

    def _orchestrate_granted(self, extension_id: str) -> bool:
        """docs/26 §0.6 阶段1：该扩展是否声明了 `spawn:orchestrate`（解锁编排原语）。"""
        from .manifest import SPAWN_ORCHESTRATE
        m = self._manifest_by_id.get(extension_id)
        return m is not None and SPAWN_ORCHESTRATE in m.capabilities

    def _memory_evolution_granted(self, extension_id: str) -> bool:
        """docs/26 G6：该扩展是否声明了 `memory:evaluate`（解锁 ctx.memory_evolution 槽）。"""
        from .manifest import MEMORY_EVALUATE
        m = self._manifest_by_id.get(extension_id)
        return m is not None and MEMORY_EVALUATE in m.capabilities

    def _build_context_fields(self, extension_id: str) -> dict:
        if not self._active:
            raise ExtensionRuntimeError(
                "extension host is not bound to a live runtime (or was invalidated)")
        thread = self._thread
        services = self._services
        agent = getattr(thread, "_agent", None)
        memory = getattr(services, "memory_service", None) if services is not None else None
        session = thread.readonly_session() if thread is not None else None
        host_model = getattr(thread, "model", "") or ""
        allowed_spawn = self._spawn_allowed_agent_types(extension_id)
        can_orchestrate = self._orchestrate_granted(extension_id)
        return dict(
            host=self,
            cwd=(services.cwd if services is not None else ""),
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
            memory_evolution=(MemoryEvolutionCap(self, thread)
                              if thread is not None and self._memory_evolution_granted(extension_id)
                              else None),
        )

    def create_context(self, extension_id: str) -> ExtensionContext:
        return ExtensionContext(**self._build_context_fields(extension_id))

    def create_command_context(self, extension_id: str) -> ExtensionCommandContext:
        fields = self._build_context_fields(extension_id)

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
        handler, ext_id = entry
        ctx = self.create_context(ext_id)
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
        handler, ext_id = self.registry.orchestrator
        ctx = self.create_context(ext_id)
        return await handler(ctx, payload)

    # ── lifecycle dispatch ────────────────────────────────────────────
    async def emit(self, event: str, payload: dict | None = None) -> None:
        """Run lifecycle handlers for an event with a per-extension-scoped context
        (docs/26 G6). Handler errors are isolated (surfaced as notices) — one bad
        handler never blocks the others."""
        handlers = self.registry.lifecycle_handlers.get(event, [])
        if not handlers:
            return
        for handler, ext_id in handlers:
            try:
                ctx = self.create_context(ext_id)
                await handler(ctx, payload or {})
            except Exception as e:  # noqa: BLE001
                # Diagnostics use an independent events sink so a per-handler ctx
                # build failure still surfaces (the scoped ctx may not exist here).
                self._notice_handler_failure(event, ext_id, e)

    def _notice_handler_failure(self, event: str, ext_id: str, exc: Exception) -> None:
        agent = getattr(self._thread, "_agent", None)
        if agent is None:
            return
        try:
            EventSink(self, agent.emit).notice(
                f"[extension {ext_id}] {event} handler failed: {exc}", level="warn")
        except Exception:  # noqa: BLE001 — a dead sink never breaks dispatch
            pass
