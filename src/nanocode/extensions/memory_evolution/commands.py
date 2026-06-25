"""memory_evolution/commands.py — slash command handlers (docs/22 §5.0.1 / §5).

Handlers are thin: parse args and hand off to the runtime. They return a plain
string (the bridge wraps it in a builtin `Local`). `/memory optimize` only
creates a host task; the model can never trigger optimization on the turn hot
path (docs/22 §4.1).
"""
from __future__ import annotations

from ..context import ExtensionCommandContext


async def run_memory_optimize_command(ctx: ExtensionCommandContext, args: str) -> str:
    """`/memory optimize [--diagnose]` — create the host optimization task."""
    tokens = (args or "").split()
    diagnose = "--diagnose" in tokens
    payload = {"diagnose": diagnose}
    return await ctx.thread.run_extension_task("memory_optimize", payload)


async def run_memory_eval_generate_command(ctx: ExtensionCommandContext, args: str) -> str:
    """`/memory eval generate` — run the EVAL-mode curator (backend-aware source)."""
    return await ctx.thread.spawn_memory_eval()
