"""memory_evolution/tasks.py — memory_optimize task handler (docs/22 §5.2 / §5.5).

Host-owned, deterministic optimization. The model never runs this; promotion is
a deterministic host gate (docs/22 §4.1). Control flow:

    no MemoryService            -> completed, diagnostic
    backend != simplemem        -> completed, explicit unsupported
    simplemem unavailable        -> failed, explicit diagnostic
    confirmed evals < threshold -> completed no-op (states need vs have)
    enough confirmed            -> baseline + bounded candidates -> promotion gate
                                   -> atomic retrieval_config.json only if improved
    --diagnose & no promotion   -> reserved diagnostician proposes candidates
                                   -> host re-evaluates -> same promotion gate

The CPU-bound optimizer runs in a worker thread and is **side-effect-free**: it
only computes an OptimizationResult. Persisting (the live-config promotion +
report) happens back on the event loop, AFTER the await returns — so a task
cancelled mid-optimize (via /task-stop) raises out of the await before any write,
and can never mutate retrieval_config.json (docs/22 review: cancel-safety).

Heavy memory deps (lancedb etc.) are imported lazily so importing the extension
package stays side-effect-free (docs/22 §9.1.3).
"""
from __future__ import annotations

from ..context import ExtensionContext


async def run_memory_optimize_task(ctx: ExtensionContext, payload: dict, *, task_id: str) -> None:
    import asyncio

    tasks = ctx.tasks
    memory = ctx.memory
    diagnose = bool((payload or {}).get("diagnose"))

    if memory is None:
        tasks.update_task(task_id, status="completed",
                          result_summary="memory_optimize: no MemoryService for this session.")
        return
    if getattr(memory, "backend_name", None) != "simplemem":
        tasks.update_task(
            task_id, status="completed",
            result_summary=(f"memory_optimize: unsupported backend "
                            f"{memory.backend_name!r} — retrieval optimization requires the "
                            f"simplemem backend (run with --memory-backend simplemem)."))
        return
    engine = getattr(memory.backend, "engine", None)
    if engine is None:
        tasks.update_task(task_id, status="failed",
                          result_summary="memory_optimize: simplemem backend has no engine.",
                          error="no engine")
        return

    # Compute (CPU-bound, no writes) off the event loop. The loop is captured so
    # the optional diagnosis sub-agent can be scheduled back onto it from the
    # worker thread (docs/22 §6). A cancellation raises here, before persisting.
    loop = asyncio.get_running_loop()
    try:
        outcome = await asyncio.to_thread(_compute_optimization, engine, diagnose, ctx, loop)
    except Exception as e:  # noqa: BLE001 — surface as a failed task, never crash host loop
        tasks.update_task(task_id, status="failed",
                          result_summary=f"memory_optimize error: {e}", error=str(e))
        return

    # Back on the loop and NOT cancelled: persist (fast I/O) + land terminal status.
    # Persist is SYNCHRONOUS (no await) so a /task-stop cancellation cannot interleave
    # between the compute result and the live-config write — once we have a result and
    # weren't cancelled at the await above, promotion completes atomically.
    kind = outcome[0]
    if kind == "noop":
        tasks.update_task(task_id, status="completed", result_summary=outcome[1])
        return
    _result, store_root = outcome[1], outcome[2]
    try:
        summary, result_path = _persist_result(store_root, _result)
    except Exception as e:  # noqa: BLE001
        tasks.update_task(task_id, status="failed",
                          result_summary=f"memory_optimize persist error: {e}", error=str(e))
        return
    tasks.update_task(task_id, status="completed", result_summary=summary,
                      result_path=result_path)


def _compute_optimization(engine, diagnose: bool, ctx, loop):
    """Side-effect-free optimizer body (worker thread). Returns one of:
      ("noop", summary)                      — insufficient confirmed evals
      ("result", OptimizationResult, store_root) — to be persisted on the loop"""
    from ...memory.maintenance import evolve_max_rounds, evolve_min_confirmed
    from ...memory.optimize import optimize_retrieval
    from ...memory.retrieval_eval import cases_from_confirmed
    from ...memory.retrieval_config_store import load_retrieval_config, store_root_for_engine

    min_confirmed = evolve_min_confirmed()
    max_rounds = evolve_max_rounds()

    cases = cases_from_confirmed()
    if len(cases) < min_confirmed:
        return ("noop",
                f"memory_optimize: not enough confirmed eval candidates "
                f"({len(cases)}/{min_confirmed}). Run `/memory eval generate`, then "
                f"`/memory eval confirm <id>` to confirm more before optimizing.")

    store_root = store_root_for_engine(engine)
    baseline = load_retrieval_config(store_root)

    diagnose_fn = None
    if diagnose and ctx is not None:
        from .agents import build_diagnose_fn
        diagnose_fn = build_diagnose_fn(ctx, loop)

    result = optimize_retrieval(engine, cases, baseline, max_rounds=max_rounds,
                                min_confirmed=min_confirmed, diagnose_fn=diagnose_fn)
    return ("result", result, store_root)


def _persist_result(store_root: str, result) -> "tuple[str, str | None]":
    """Persist a computed OptimizationResult (event-loop side, post-cancel-check).

    Returns (result_summary, result_path). On promotion this is the sole writer of
    the live retrieval_config.json; the audit report (run dir + history) is written
    on both paths. result_path always points at the run summary audit artifact."""
    from ...memory.retrieval_config_store import save_retrieval_config, run_summary_path

    if result.promoted:
        cfg_path = save_retrieval_config(store_root, result.best_config,
                                         run_id=result.run_id, report=result.to_report())
        summary = (f"memory_optimize: promoted new retrieval config "
                   f"(baseline {result.baseline_score:.4f} -> {result.best_score:.4f}, "
                   f"+{result.best_score - result.baseline_score:.4f}). Wrote {cfg_path}.")
        return summary, str(run_summary_path(store_root, result.run_id))

    save_retrieval_config(store_root, None, run_id=result.run_id, report=result.to_report())
    summary = (f"memory_optimize: no promotion (baseline {result.baseline_score:.4f}, "
               f"best candidate {result.best_score:.4f}; {result.no_promotion_reason}). "
               f"Live config unchanged.")
    return summary, str(run_summary_path(store_root, result.run_id))
