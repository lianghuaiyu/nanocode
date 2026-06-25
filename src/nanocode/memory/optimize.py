"""memory/optimize.py — deterministic host retrieval optimizer (docs/22 §5.2 / Phase 4).

Bounded, no-LLM hill-climbing over `RetrievalConfig` against confirmed eval cases.
The optimizer NEVER writes the live config; it returns an `OptimizationResult`
and the caller's promotion gate (here, `OptimizationResult.promoted` computed by
the deterministic final gate) decides whether the host persists it. Candidate
evaluation is pure (no model). An optional `diagnose_fn` (Phase 6) may propose
extra candidate configs when deterministic search stalls — but those are only
*candidate sources*; they go through the same deterministic gate, never a
self-validated write (docs/22 §4.3 / §5.3).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from .engines.simplemem.retrieval_config import FUSION_MODES, RetrievalConfig
from .retrieval_eval import RetrievalEvalCase, RetrievalEvalResult, score_case

# Promotion thresholds (deterministic gate). Conservative: a candidate must beat
# the baseline by a margin, never increase zero-retrieval cases, not regress the
# low tail, and (with enough data) not regress a held-out split.
MIN_DELTA = 0.01
P10_TOLERANCE = 0.02
HOLDOUT_TOLERANCE = 0.02
HOLDOUT_MIN_CASES = 10


@dataclass(frozen=True)
class AggregateScore:
    n: int
    mean: float
    p10: float
    zero_retrieval_count: int
    results: tuple[RetrievalEvalResult, ...] = ()

    @classmethod
    def from_results(cls, results: list[RetrievalEvalResult]) -> "AggregateScore":
        n = len(results)
        if not n:
            return cls(0, 0.0, 0.0, 0, ())
        scores = sorted(r.score for r in results)
        mean = sum(scores) / n
        p10 = scores[max(0, int(0.1 * (n - 1)))]
        zero = sum(1 for r in results if r.hit_count == 0)
        return cls(n, mean, p10, zero, tuple(results))


@dataclass(frozen=True)
class RejectedMove:
    field: str
    to_value: object
    reason: str
    mean: float


@dataclass(frozen=True)
class RoundReport:
    index: int
    candidates_evaluated: int
    accepted_field: "str | None"
    best_mean: float


@dataclass(frozen=True)
class OptimizationResult:
    promoted: bool
    baseline_score: float
    best_score: float
    best_config: RetrievalConfig
    baseline_config: RetrievalConfig
    run_id: str
    no_promotion_reason: str = ""
    rounds: tuple[RoundReport, ...] = ()
    rejected: tuple[RejectedMove, ...] = ()
    report_results: tuple[RetrievalEvalResult, ...] = ()
    report_path: "str | None" = None

    def to_report(self) -> dict:
        return {
            "summary": {
                "run_id": self.run_id,
                "promoted": self.promoted,
                "baseline_score": self.baseline_score,
                "best_score": self.best_score,
                "delta": self.best_score - self.baseline_score,
                "no_promotion_reason": self.no_promotion_reason,
                "baseline_config": self.baseline_config.to_dict(),
                "best_config": self.best_config.to_dict(),
                "rounds": [r.__dict__ for r in self.rounds],
                "rejected": [r.__dict__ for r in self.rejected],
            },
            "cases": [r.to_dict() for r in self.report_results],
            "history": {
                "run_id": self.run_id,
                "promoted": self.promoted,
                "baseline_score": round(self.baseline_score, 6),
                "best_score": round(self.best_score, 6),
                "delta": round(self.best_score - self.baseline_score, 6),
                "n": self.best_config and len(self.report_results),
                "reason": self.no_promotion_reason,
            },
        }


# ── evaluation ────────────────────────────────────────────────────────

def evaluate_config(engine, config: RetrievalConfig,
                    cases: list[RetrievalEvalCase]) -> AggregateScore:
    """Score `config` over `cases` using the engine's no-LLM retrieval."""
    results = []
    for case in cases:
        hits = engine.retrieve_with_config(case.question, config, limit=config.max_context)
        results.append(score_case(case, hits))
    return AggregateScore.from_results(results)


# ── candidate generation (bounded) ─────────────────────────────────────

_INT_KNOBS = (
    ("semantic_top_k", 5, 0, 40),
    ("keyword_top_k", 3, 0, 20),
    ("structured_top_k", 3, 0, 15),
    ("max_context", 2, 3, 10),
)
_WEIGHT_KNOBS = (
    "weight_semantic", "weight_keyword", "weight_structured_person",
    "weight_structured_entity", "weight_timestamp", "lexical_exact_boost",
)
_WEIGHT_STEP = 0.25
_WEIGHT_LO, _WEIGHT_HI = 0.0, 3.0


def _neighbors(cfg: RetrievalConfig) -> list[tuple[str, object, RetrievalConfig]]:
    """Single-field bounded perturbations of `cfg` (1 change each, for attribution)."""
    out: list[tuple[str, object, RetrievalConfig]] = []

    def add(field_name: str, value) -> None:
        try:
            cand = replace(cfg, **{field_name: value})
        except Exception:
            return
        out.append((field_name, value, cand))

    for name, step, lo, hi in _INT_KNOBS:
        cur = getattr(cfg, name)
        for nv in (cur - step, cur + step):
            if lo <= nv <= hi and nv != cur:
                add(name, nv)
    for m in FUSION_MODES:
        if m != cfg.fusion_mode:
            add("fusion_mode", m)
    for name in _WEIGHT_KNOBS:
        cur = getattr(cfg, name)
        for nv in (round(cur - _WEIGHT_STEP, 4), round(cur + _WEIGHT_STEP, 4)):
            if _WEIGHT_LO <= nv <= _WEIGHT_HI and abs(nv - cur) > 1e-9:
                add(name, nv)
    return out


def _key(cfg: RetrievalConfig) -> tuple:
    return tuple(sorted(cfg.to_dict().items()))


# ── gates ──────────────────────────────────────────────────────────────

def _beats(challenger: AggregateScore, incumbent: AggregateScore, *,
           min_delta: float) -> "tuple[bool, str]":
    if challenger.mean < incumbent.mean + min_delta:
        return False, f"mean {challenger.mean:.4f} < {incumbent.mean:.4f}+{min_delta}"
    if challenger.zero_retrieval_count > incumbent.zero_retrieval_count:
        return False, (f"zero_retrieval {challenger.zero_retrieval_count} > "
                       f"{incumbent.zero_retrieval_count}")
    if challenger.p10 < incumbent.p10 - P10_TOLERANCE:
        return False, f"p10 {challenger.p10:.4f} < {incumbent.p10:.4f}-{P10_TOLERANCE}"
    return True, ""


def _holdout_ok(best_hold: AggregateScore, baseline_hold: AggregateScore) -> "tuple[bool, str]":
    """Anti-overfit: the held-out split must not regress on mean, zero-retrieval,
    or the low tail (docs/22 review: holdout needs the same regression guards as
    the train gate, not just mean)."""
    if best_hold.mean < baseline_hold.mean - HOLDOUT_TOLERANCE:
        return False, f"mean {best_hold.mean:.4f} < {baseline_hold.mean:.4f}-{HOLDOUT_TOLERANCE}"
    if best_hold.zero_retrieval_count > baseline_hold.zero_retrieval_count:
        return False, (f"zero_retrieval {best_hold.zero_retrieval_count} > "
                       f"{baseline_hold.zero_retrieval_count}")
    if best_hold.p10 < baseline_hold.p10 - P10_TOLERANCE:
        return False, f"p10 {best_hold.p10:.4f} < {baseline_hold.p10:.4f}-{P10_TOLERANCE}"
    return True, ""


# ── split ──────────────────────────────────────────────────────────────

def _split(cases: list[RetrievalEvalCase]):
    """Deterministic train/holdout split (every 5th case held out) once there are
    enough cases; otherwise train == holdout == all (no separate holdout)."""
    if len(cases) < HOLDOUT_MIN_CASES:
        return list(cases), list(cases)
    ordered = sorted(cases, key=lambda c: c.id)
    holdout = [c for i, c in enumerate(ordered) if i % 5 == 0]
    train = [c for i, c in enumerate(ordered) if i % 5 != 0]
    return train, holdout


# ── main loop ───────────────────────────────────────────────────────────

def optimize_retrieval(engine, eval_cases: list[RetrievalEvalCase],
                       current: RetrievalConfig, *, max_rounds: int,
                       min_confirmed: int, diagnose_fn=None) -> OptimizationResult:
    run_id = "run-" + uuid.uuid4().hex[:12]
    n = len(eval_cases)
    train, holdout = _split(eval_cases)
    baseline_train = evaluate_config(engine, current, train)
    baseline_hold = evaluate_config(engine, current, holdout)

    best_config = current
    best_train = baseline_train
    seen = {_key(current)}
    rounds: list[RoundReport] = []
    rejected: list[RejectedMove] = []

    def _try_candidates(cands):
        nonlocal best_config, best_train
        scored = []
        for field_name, value, cand in cands:
            k = _key(cand)
            if k in seen:
                continue
            seen.add(k)
            agg = evaluate_config(engine, cand, train)
            scored.append((field_name, value, cand, agg))
        scored.sort(key=lambda x: x[3].mean, reverse=True)
        for field_name, value, cand, agg in scored:
            ok, reason = _beats(agg, best_train, min_delta=MIN_DELTA)
            if ok:
                best_config, best_train = cand, agg
                return field_name, len(scored)
            rejected.append(RejectedMove(field_name, value, reason, agg.mean))
        return None, len(scored)

    for rnd in range(max_rounds):
        accepted_field, n_cands = _try_candidates(_neighbors(best_config))
        rounds.append(RoundReport(rnd, n_cands, accepted_field, best_train.mean))
        if accepted_field is None:
            break

    # docs/22 §5.3 Phase 6: optional LLM-assisted diagnosis when deterministic
    # search stalled. Suggestions become candidate configs scored by the SAME
    # deterministic gate — never a self-validated write.
    if diagnose_fn is not None and _key(best_config) == _key(current):
        suggestions = diagnose_fn(_failure_report(current, baseline_train, rejected))
        cand_configs = materialize_suggestions(current, suggestions)
        accepted_field, _ = _try_candidates(
            [("diagnosis", None, c) for c in cand_configs])
        rounds.append(RoundReport(len(rounds), len(cand_configs),
                                  accepted_field, best_train.mean))

    # Final promotion gate vs baseline (+ holdout anti-overfit when enough data).
    promoted = _key(best_config) != _key(current)
    reason = ""
    if promoted:
        ok, reason = _beats(best_train, baseline_train, min_delta=MIN_DELTA)
        promoted = ok
        if promoted and n >= HOLDOUT_MIN_CASES:
            best_hold = evaluate_config(engine, best_config, holdout)
            ok_h, reason_h = _holdout_ok(best_hold, baseline_hold)
            if not ok_h:
                promoted = False
                reason = f"holdout regressed ({reason_h})"
    else:
        reason = "no candidate beat the baseline"

    report_results = evaluate_config(engine, best_config, eval_cases).results
    return OptimizationResult(
        promoted=promoted,
        baseline_score=baseline_train.mean,
        best_score=best_train.mean,
        best_config=best_config,
        baseline_config=current,
        run_id=run_id,
        no_promotion_reason="" if promoted else reason,
        rounds=tuple(rounds),
        rejected=tuple(rejected),
        report_results=report_results,
    )


# ── diagnosis support (Phase 6) ────────────────────────────────────────

# Fields a diagnostician may propose (the no-LLM retrieval action space only),
# each with its accepted range. Out-of-range / unknown -> reject (docs/22 §5.5).
_SUGGESTION_BOUNDS = {
    "semantic_top_k": (0, 40),
    "keyword_top_k": (0, 20),
    "structured_top_k": (0, 15),
    "max_context": (3, 10),
    "weight_semantic": (0.0, 3.0),
    "weight_keyword": (0.0, 3.0),
    "weight_structured_person": (0.0, 3.0),
    "weight_structured_entity": (0.0, 3.0),
    "weight_timestamp": (0.0, 3.0),
    "lexical_exact_boost": (0.0, 3.0),
    "time_decay_half_life_days": (0.0, 3650.0),  # excludes 0 via the >low check below
}
ALLOWED_SUGGESTION_FIELDS = frozenset(_SUGGESTION_BOUNDS) | {"fusion_mode"}


def _in_range(field_name: str, value) -> bool:
    if field_name == "fusion_mode":
        return value in FUSION_MODES
    if field_name == "time_decay_half_life_days" and value is None:
        return True
    lo, hi = _SUGGESTION_BOUNDS[field_name]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if field_name == "time_decay_half_life_days":
        return lo < value <= hi
    return lo <= value <= hi


def _failure_report(current: RetrievalConfig, baseline: AggregateScore,
                    rejected: list[RejectedMove]) -> dict:
    worst = sorted(baseline.results, key=lambda r: r.score)[:10]
    return {
        "current_config": current.to_dict(),
        "baseline_mean": baseline.mean,
        "baseline_p10": baseline.p10,
        "zero_retrieval_count": baseline.zero_retrieval_count,
        "worst_cases": [r.to_dict() for r in worst],
        "rejected_moves": [{"field": r.field, "to_value": r.to_value} for r in rejected],
    }


def materialize_suggestions(current: RetrievalConfig,
                            suggestions: "list[dict] | None") -> list[RetrievalConfig]:
    """Turn validated parameter suggestions into candidate configs (docs/22 §5.5).

    Each suggestion is a dict of field→value. A suggestion is rejected entirely
    if it names an unknown field or any value is out of the action-space range
    (fail closed). Surviving suggestions still pass `RetrievalConfig.validate()`.
    The host — not the agent — materializes and re-evaluates these."""
    out: list[RetrievalConfig] = []
    for sug in (suggestions or []):
        if not isinstance(sug, dict) or not sug:
            continue
        if any(k not in ALLOWED_SUGGESTION_FIELDS for k in sug):
            continue  # unknown field -> reject whole suggestion
        if any(not _in_range(k, v) for k, v in sug.items()):
            continue  # out-of-range -> reject whole suggestion
        try:
            cand = replace(current, **sug)
            cand.validate()
        except Exception:
            continue
        out.append(cand)
    return out
