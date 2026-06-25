"""memory_evolution/agents.py — diagnosis plane bridge (docs/22 §5.3 / §6).

`build_diagnose_fn(ctx, loop)` returns a synchronous `diagnose_fn(failure_report)`
that the (worker-thread) optimizer can call. It spawns the reserved
`memory-retrieval-diagnostician` hidden agent on the main event loop, parses its
strict-JSON parameter suggestions, and returns them as candidate-source dicts.

The diagnostician is proposal-only: its output is a *candidate source*, never a
config mutation. The host validates/clamps suggestions and re-evaluates them
through the same deterministic promotion gate (docs/22 §5.3).
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import TimeoutError as FuturesTimeout

from .manifest import MEMORY_DIAGNOSIS_ROLE, MEMORY_DIAGNOSTICIAN_TYPE

_DIAGNOSE_TIMEOUT_MS = 120_000


def build_diagnose_fn(ctx, loop):
    """Return a sync diagnose_fn for the optimizer (runs in a worker thread)."""

    def diagnose_fn(failure_report: dict) -> list[dict]:
        try:
            model = ctx.models.resolve(MEMORY_DIAGNOSIS_ROLE)
        except Exception:
            model = None
        prompt = ("Diagnose this retrieval failure report and propose parameter "
                  "adjustments.\n\n" + json.dumps(failure_report, ensure_ascii=False, indent=2))
        try:
            fut = asyncio.run_coroutine_threadsafe(
                ctx.thread.run_reserved_subagent(
                    MEMORY_DIAGNOSTICIAN_TYPE, prompt, model=model,
                    timeout_ms=_DIAGNOSE_TIMEOUT_MS),
                loop)
            text = fut.result(timeout=_DIAGNOSE_TIMEOUT_MS / 1000.0 + 30)
        except FuturesTimeout:
            # Don't leak a still-running diagnostician: cancel the scheduled future
            # (the reserved agent also has its own bounded run_once timeout).
            fut.cancel()
            return []
        except Exception:
            return []  # diagnosis is best-effort; failure never blocks optimization
        return _parse_suggestions(text)

    return diagnose_fn


def _parse_suggestions(text: str) -> list[dict]:
    from ...memory.maintenance import extract_json_object
    try:
        data = json.loads(extract_json_object(text or ""))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    ps = data.get("parameter_suggestions")
    if isinstance(ps, dict) and ps:
        return [ps]
    return []
