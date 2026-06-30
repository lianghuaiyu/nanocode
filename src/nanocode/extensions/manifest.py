"""extensions/manifest.py — typed manifest + contribution schema (docs/22 §7 Phase 0).

A `manifest` declares *what* an extension contributes; it is pure data, not
runtime code. The Pi mental model (`docs/22 §5.0.1`): a manifest names resource
entry points (commands / task kinds / hidden agents / lifecycle events / model
roles) and a capability set; the activation factory (`extension.py::activate`)
is what actually registers those contributions into the host registries.

First version is system-only — no project/user extension discovery. The schema
keeps the Pi contribution names so a future project/user loader can reuse it
unchanged (with added trust/capability gates).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# docs/26 阶段1 ②：受信 spawn 槽的 capability token。声明此 capability 的扩展，其 ctx 会被
# 挂上 `ctx.spawn`（仅能 spawn 该扩展自己贡献的 reserved/hidden agent；子 caps 由内核派生）。
SPAWN_RESERVED = "spawn:reserved"

# docs/26 §0.6 阶段1：编排级受信 spawn 槽 capability。声明此 capability 的扩展，其 `ctx.spawn`
# 额外解锁**非提权**的编排原语（run/run_background/new_group/cancel_group）——可 spawn 通用
# 模型类型(general/coder/explore/plan/custom，**拒 reserved**)，子工具/sandbox 仍由内核派生。
# 仅授予 first-party orchestration 扩展（编排策略上提层④的命门，O5）。
SPAWN_ORCHESTRATE = "spawn:orchestrate"

# docs/26 G6 收口：memory-evolution 宿主操作槽 capability。声明此 capability 的扩展，其 ctx 会
# 被挂上 `ctx.memory_evolution`（仅 run_optimization / eval_generate 两个宿主操作）——取代经
# `ctx.thread` 直达整个 RuntimeThread facade 的能力泄漏。仅授予 first-party memory_evolution 扩展。
MEMORY_EVALUATE = "memory:evaluate"


@dataclass(frozen=True)
class CommandContribution:
    """One slash command an extension contributes.

    `match` mirrors the builtin command registry semantics (registry.py): an
    `exact` command fires only on `line == name`; `exact_or_prefix` also fires on
    `name + " " + args`.
    """
    name: str
    match: Literal["exact", "exact_or_prefix", "prefix"] = "exact"
    description: str = ""
    arg_hint: str = ""


@dataclass(frozen=True)
class McpServerSpec:
    """An out-of-process MCP server an extension declares (docs/26 G6).

    Pure declarative data: the host spawns `command args` as a SEPARATE subprocess
    (mcp/connection.py) and routes its tools in through the normal MCP path
    (source=MCP, trust=UNTRUSTED, sealed all-None ctx). This is the only execution
    path an `untrusted` extension has — it never runs in-process code.

    `env` is a tuple-of-pairs (not a dict) so the dataclass stays frozen/hashable."""
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ExtensionContributes:
    """Declarative contribution surface (no runtime code)."""
    commands: tuple[CommandContribution, ...] = ()
    task_kinds: tuple[str, ...] = ()
    hidden_agents: tuple[str, ...] = ()
    lifecycle_events: tuple[str, ...] = ()
    model_roles: tuple[str, ...] = ()
    mcp_servers: tuple[McpServerSpec, ...] = ()


@dataclass(frozen=True)
class ExtensionManifest:
    """A built-in system extension manifest, or an untrusted declarative extension.

    `kind="system"` (first-party): `entrypoint` is a `module:function` string the
    `ExtensionHost` resolves; its factory registers in-process contributions.

    `kind="untrusted"` (docs/26 G6): declarative-only — NO entrypoint, NO in-process
    contributions, NO capabilities; may only declare `mcp_servers` (executed
    out-of-process). Such manifests are never activated; their servers surface via
    `ExtensionHost.mcp_contributions()`."""
    id: str
    kind: Literal["system", "untrusted"]
    entrypoint: str = ""
    contributes: ExtensionContributes = field(default_factory=ExtensionContributes)
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        c = self.contributes
        if self.kind == "system":
            if ":" not in self.entrypoint:
                raise ValueError(
                    f"extension {self.id!r}: entrypoint must be 'module:function', "
                    f"got {self.entrypoint!r}")
        elif self.kind == "untrusted":
            if self.entrypoint:
                raise ValueError(
                    f"extension {self.id!r}: untrusted extensions must NOT declare an "
                    f"entrypoint (no in-process code; declare an mcp_server instead)")
            if c.commands or c.task_kinds or c.hidden_agents or c.lifecycle_events or c.model_roles:
                raise ValueError(
                    f"extension {self.id!r}: untrusted extensions may only contribute "
                    f"mcp_servers (no commands/task_kinds/hidden_agents/lifecycle/model_roles)")
            if self.capabilities:
                raise ValueError(
                    f"extension {self.id!r}: untrusted extensions must declare no capabilities")
            if not c.mcp_servers:
                raise ValueError(
                    f"extension {self.id!r}: untrusted extension declares nothing "
                    f"(empty mcp_servers)")
        else:
            raise ValueError(
                f"extension {self.id!r}: unknown kind {self.kind!r} "
                f"(expected 'system' or 'untrusted')")
