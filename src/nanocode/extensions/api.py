"""extensions/api.py — registration-only ExtensionAPI (docs/22 §7 Phase 0 / §5.0.1).

Mirrors Pi's `createExtensionAPI()`: the activation factory receives this object
and calls `on / register_command / register_task_kind / register_hidden_agent /
register_model_role / register_tool` to write contributions into the host
registry. Activation must ONLY register — it must not touch `MemoryService`, read
env, or start background work (those are deferred to bind/run time).

`register_tool` (docs/24 Phase 4b) registers an LLM-callable tool, but the raw
handler is NEVER handed to the model: ExtensionHost wraps it through a
`tools.spec.Tool` (namespaced `ext__<id>__name`, source=EXT, trust=UNTRUSTED,
needs=declared) and the dispatch chokepoint (CapabilityRouter / PermissionEngine)
authorizes + mints a sealed `ToolContext` for it (UNTRUSTED ⟹ every capability
slot None — it cannot reach fs/exec/spawn/memory). This is Pi's
tool-definition-wrapper equivalent.
"""
from __future__ import annotations

from .manifest import CommandContribution
from .registry import (
    CommandHandler, ContributionRegistry, HiddenAgentProfile, LifecycleHandler,
    ModelRolePolicy, OrchestratorHandler, TaskHandler, ToolHandler,
)


class ExtensionAPI:
    """The object passed to `activate(api)`. Registration-only surface."""

    def __init__(self, registry: ContributionRegistry, *, extension_id: str) -> None:
        self._registry = registry
        self._extension_id = extension_id

    def on(self, event: str, handler: LifecycleHandler) -> None:
        self._registry.add_lifecycle(event, handler, extension_id=self._extension_id)

    def register_command(self, spec: CommandContribution, handler: CommandHandler) -> None:
        self._registry.add_command(spec, handler, extension_id=self._extension_id)

    def register_task_kind(self, kind: str, handler: TaskHandler) -> None:
        self._registry.add_task_kind(kind, handler, extension_id=self._extension_id)

    def register_orchestrator(self, handler: OrchestratorHandler) -> None:
        """Register the single orchestration handler (docs/26 §0.6 阶段1).

        `handler(ctx, payload) -> str` owns chain/parallel policy and drives subagents
        through `ctx.spawn.*` (kernel derives child caps). Only one orchestrator may be
        registered host-wide; a second registration fails loud."""
        self._registry.add_orchestrator(handler, extension_id=self._extension_id)

    def register_hidden_agent(self, profile: HiddenAgentProfile) -> None:
        self._registry.add_hidden_agent(profile, extension_id=self._extension_id)

    def register_model_role(self, role: str, policy: ModelRolePolicy) -> None:
        self._registry.add_model_role(role, policy, extension_id=self._extension_id)

    def register_tool(self, spec: dict, handler: ToolHandler, *,
                      needs: "frozenset | set | None" = None) -> None:
        """Register an LLM-callable tool (docs/24 Phase 4b).

        `spec` is the model-visible schema dict (`{"name", "description",
        "input_schema"}`); `handler` is an async callable `handler(inp, ctx)` that
        runs with a sealed UNTRUSTED ToolContext. `needs` is the capability
        declaration; under the UNTRUSTED trust policy the granted set is ∅, so the
        handler's ctx slots are all None regardless. The raw handler never reaches
        the model — ExtensionHost wraps it through CapabilityRouter."""
        name = spec.get("name") if isinstance(spec, dict) else None
        if not name:
            from .errors import ExtensionLoadError
            raise ExtensionLoadError(
                f"extension {self._extension_id!r}: register_tool requires a schema "
                f"with a 'name' field")
        self._registry.add_tool(name, spec, handler,
                                needs=frozenset(needs or frozenset()),
                                extension_id=self._extension_id)
