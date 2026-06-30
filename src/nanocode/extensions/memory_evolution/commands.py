"""memory_evolution/commands.py — slash command handlers (docs/22 §5.0.1 / §5).

Handlers are thin: parse args and hand off to the runtime through the narrow
`ctx.memory_evolution` capability (docs/26 G6 — handlers never touch a raw
RuntimeThread). They return a plain string (the bridge wraps it in a builtin
`Local`). `/memory optimize` only creates a host task; the model can never
trigger optimization on the turn hot path (docs/22 §4.1).
"""
from __future__ import annotations

from ..context import ExtensionCommandContext

_UNAVAILABLE = "memory evolution extension is not available for this session."


async def run_memory_optimize_command(ctx: ExtensionCommandContext, args: str) -> str:
    """`/memory optimize [--diagnose]` — create the host optimization task."""
    if ctx.memory_evolution is None:
        return _UNAVAILABLE
    diagnose = "--diagnose" in (args or "").split()
    return await ctx.memory_evolution.run_optimization(diagnose=diagnose)


async def run_memory_eval_generate_command(ctx: ExtensionCommandContext, args: str) -> str:
    """`/memory eval generate` — run the EVAL-mode curator (backend-aware source)."""
    if ctx.memory_evolution is None:
        return _UNAVAILABLE
    return await ctx.memory_evolution.eval_generate()
