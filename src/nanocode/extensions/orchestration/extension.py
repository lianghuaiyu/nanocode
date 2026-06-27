"""orchestration/extension.py — activation factory (docs/26 §0.6 阶段1).

`activate(api)` only registers the single orchestration handler; it must not access
host services or start background work (那些发生在 run 时的 call-time context)。"""
from __future__ import annotations

from ..api import ExtensionAPI
from .policy import orchestrate


def activate(api: ExtensionAPI) -> None:
    api.register_orchestrator(orchestrate)
