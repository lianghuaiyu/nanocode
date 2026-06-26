"""extensions/registry.py — host registries + fail-loud conflict rules (docs/22 §7 Phase 0).

The activation factory writes contributions here via `ExtensionAPI`. Conflicts
are surfaced at startup as `ExtensionLoadError` — never silently dropped:

1. command name collides with a non-replaceable builtin / another extension → fail.
2. two extensions register the same task kind → fail.
3. hidden agent type collides with a reserved/custom agent type → fail.

Registries hold only contributions; the runner (`ExtensionHost`) supplies the
call-time context when it invokes a handler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .errors import ExtensionLoadError
from .manifest import CommandContribution

# Handler signatures. Command/task handlers receive a call-time context built by
# the host (typed in context.py; kept as `object` here to avoid an import cycle).
CommandHandler = Callable[..., Awaitable[object]]
TaskHandler = Callable[..., Awaitable[None]]
LifecycleHandler = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class HiddenAgentProfile:
    """A reserved hidden system agent contributed by an extension (docs/22 §6).

    The agent itself is defined as a reserved type in `agents/registry.py`
    (tools=[], background, max_turns=1, not model-spawnable, not project-overridable).
    This profile only records the contribution + which model role drives it."""
    agent_type: str
    description: str = ""
    model_role: str | None = None


@dataclass(frozen=True)
class ModelRolePolicy:
    """How an extension model role resolves to a concrete model (docs/22 §5.4).

    `default` is the fallback when no env override is set: "host" means "use the
    host's current model". `env_var`, when set, is a host-only env knob that may
    override the model id (read by the host, never by the engine)."""
    default: str = "host"
    env_var: str | None = None


@dataclass(frozen=True)
class RegisteredCommand:
    contribution: CommandContribution
    handler: CommandHandler
    extension_id: str


# docs/24 Phase 4b：扩展贡献的 LLM-callable 工具。handler 是扩展的回调；它**绝不**被裸交给
# 模型——ExtensionHost 把它包成 tools.spec.Tool（namespace=ext__<id>__name、source=EXT、
# trust=UNTRUSTED、needs=declared），经 dispatch 咽喉点授权 + 按 trust 铸 ctx（UNTRUSTED →
# ctx 全 None 把手，够不到 fs/exec/spawn/memory）后才执行。
ToolHandler = Callable[..., Awaitable[object]]


@dataclass(frozen=True)
class RegisteredTool:
    name: str                       # 扩展声明的裸名（未加 ext__<id>__ 前缀）
    schema: dict                    # 模型可见入参 schema
    handler: ToolHandler
    needs: frozenset                # 声明能力（UNTRUSTED 策略下交集为 ∅）
    extension_id: str


@dataclass
class ContributionRegistry:
    commands: dict[str, RegisteredCommand] = field(default_factory=dict)
    task_kinds: dict[str, tuple[TaskHandler, str]] = field(default_factory=dict)
    hidden_agents: dict[str, tuple[HiddenAgentProfile, str]] = field(default_factory=dict)
    lifecycle_handlers: dict[str, list[tuple[LifecycleHandler, str]]] = field(default_factory=dict)
    model_roles: dict[str, tuple[ModelRolePolicy, str]] = field(default_factory=dict)
    tools: dict[str, RegisteredTool] = field(default_factory=dict)

    # ── command ───────────────────────────────────────────────────────
    # Note: builtin-vs-extension command collisions are enforced by the
    # entrypoints bridge (which knows the builtin registry), not here — that
    # keeps extensions/ free of an entrypoints import. This only guards
    # extension-vs-extension collisions.
    def add_command(self, c: CommandContribution, handler: CommandHandler, *,
                    extension_id: str) -> None:
        existing = self.commands.get(c.name)
        if existing is not None:
            raise ExtensionLoadError(
                f"extension {extension_id!r}: command {c.name!r} already registered "
                f"by extension {existing.extension_id!r}")
        self.commands[c.name] = RegisteredCommand(c, handler, extension_id)

    # ── task kind ─────────────────────────────────────────────────────
    def add_task_kind(self, kind: str, handler: TaskHandler, *, extension_id: str) -> None:
        existing = self.task_kinds.get(kind)
        if existing is not None:
            raise ExtensionLoadError(
                f"extension {extension_id!r}: task kind {kind!r} already registered "
                f"by extension {existing[1]!r}")
        self.task_kinds[kind] = (handler, extension_id)

    # ── hidden agent ──────────────────────────────────────────────────
    def add_hidden_agent(self, profile: HiddenAgentProfile, *, extension_id: str) -> None:
        # Only the extension-vs-extension dup check happens at activation (pure).
        # The hidden-vs-custom-agent collision check is deferred to bind_runtime
        # (a host/trust phase), so activation never reads env/project agent files
        # (docs/22 §5.0.1: activation is registration-only).
        name = profile.agent_type
        existing = self.hidden_agents.get(name)
        if existing is not None:
            raise ExtensionLoadError(
                f"extension {extension_id!r}: hidden agent {name!r} already registered "
                f"by extension {existing[1]!r}")
        self.hidden_agents[name] = (profile, extension_id)

    # ── lifecycle ─────────────────────────────────────────────────────
    def add_lifecycle(self, event: str, handler: LifecycleHandler, *, extension_id: str) -> None:
        self.lifecycle_handlers.setdefault(event, []).append((handler, extension_id))

    # ── model role ────────────────────────────────────────────────────
    def add_model_role(self, role: str, policy: ModelRolePolicy, *, extension_id: str) -> None:
        existing = self.model_roles.get(role)
        if existing is not None:
            raise ExtensionLoadError(
                f"extension {extension_id!r}: model role {role!r} already registered "
                f"by extension {existing[1]!r}")
        self.model_roles[role] = (policy, extension_id)

    # ── tool (docs/24 Phase 4b) ───────────────────────────────────────
    def add_tool(self, name: str, schema: dict, handler: ToolHandler, *,
                 needs: frozenset, extension_id: str) -> None:
        """登记一个扩展工具贡献（dup fail-loud，按裸名）。

        裸名（未加前缀）冲突即抛——namespace 前缀 ext__<id>__ 在 ExtensionHost 包成 Tool 时加。
        reserved-builtin / forced-namespace / override 规则最终由 ToolRegistry.register 兜底
        （工具进 agent overlay 时再判）。"""
        existing = self.tools.get(name)
        if existing is not None:
            raise ExtensionLoadError(
                f"extension {extension_id!r}: tool {name!r} already registered "
                f"by extension {existing.extension_id!r}")
        self.tools[name] = RegisteredTool(
            name=name, schema=schema, handler=handler,
            needs=frozenset(needs), extension_id=extension_id)
