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
class ExtensionContributes:
    """Declarative contribution surface (no runtime code)."""
    commands: tuple[CommandContribution, ...] = ()
    task_kinds: tuple[str, ...] = ()
    hidden_agents: tuple[str, ...] = ()
    lifecycle_events: tuple[str, ...] = ()
    model_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtensionManifest:
    """A built-in system extension manifest.

    `entrypoint` is a `module:function` string resolved by `ExtensionHost`. The
    factory it names receives an `ExtensionAPI` and registers the declared
    contributions — it must not start background work or touch host services."""
    id: str
    kind: Literal["system"]
    entrypoint: str
    contributes: ExtensionContributes = field(default_factory=ExtensionContributes)
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.kind != "system":
            raise ValueError(
                f"extension {self.id!r}: only kind='system' is supported in the "
                f"first version (no project/user extensions)")
        if ":" not in self.entrypoint:
            raise ValueError(
                f"extension {self.id!r}: entrypoint must be 'module:function', "
                f"got {self.entrypoint!r}")
