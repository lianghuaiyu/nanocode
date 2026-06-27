"""orchestration/manifest.py — built-in system extension manifest (docs/26 §0.6 阶段1).

声明 `spawn:orchestrate` capability → 其 ctx.spawn 解锁编排级原语（run_fresh/run_step/
run_background/new_group/cancel_group/launch_coordinator，子 caps 仍内核派生，非提权）。
无 tools / commands / hidden agents / model roles —— 唯一贡献是 orchestrator handler。"""
from __future__ import annotations

from ..manifest import ExtensionContributes, ExtensionManifest, SPAWN_ORCHESTRATE

MANIFEST = ExtensionManifest(
    id="nanocode.orchestration",
    kind="system",
    entrypoint="nanocode.extensions.orchestration.extension:activate",
    contributes=ExtensionContributes(),
    capabilities=frozenset({SPAWN_ORCHESTRATE}),
)
