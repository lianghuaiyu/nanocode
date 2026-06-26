"""docs/22 Phase 4: deterministic host optimizer + retrieval_config_store."""
from dataclasses import dataclass

import pytest

from nanocode.memory.engines.simplemem.retrieval_config import RetrievalConfig
from nanocode.memory.optimize import (
    optimize_retrieval, evaluate_config, materialize_suggestions,
)
from nanocode.memory.retrieval_eval import RetrievalEvalCase
from nanocode.memory import retrieval_config_store as RCS


@dataclass
class _Hit:
    entry_id: str
    lossless_restatement: str
    keywords: tuple = ()


class _FakeEngine:
    """retrieve_with_config returns hits driven by `hits_for(query, config)`."""

    def __init__(self, hits_for, root="/tmp/fake-store"):
        self._hits_for = hits_for
        self._root = root

    def stats(self):
        return {"root": self._root}

    def retrieve_with_config(self, query, config, *, limit):
        return self._hits_for(query, config)[:limit]


def _cases(n):
    return [RetrievalEvalCase(id=f"c{i}", question=f"q{i}", answer=f"answer {i} here",
                              evidence=(f"answer {i} here",)) for i in range(n)]


def test_promotion_when_a_candidate_improves():
    cases = _cases(5)
    ans = {c.question: c.answer for c in cases}

    def hits_for(q, cfg):
        # perfect hit only when semantic_top_k bumped above baseline (25 -> 30)
        if cfg.semantic_top_k >= 30:
            return [_Hit("e", ans[q])]
        return [_Hit("e", "irrelevant filler")]

    res = optimize_retrieval(_FakeEngine(hits_for), cases, RetrievalConfig(),
                             max_rounds=3, min_confirmed=5)
    assert res.promoted is True
    assert res.best_config.semantic_top_k == 30
    assert res.best_score > res.baseline_score
    assert res.run_id.startswith("run-")


def test_no_promotion_when_nothing_improves():
    cases = _cases(5)

    def hits_for(q, cfg):
        return [_Hit("e", "always irrelevant")]

    res = optimize_retrieval(_FakeEngine(hits_for), cases, RetrievalConfig(),
                             max_rounds=3, min_confirmed=5)
    assert res.promoted is False
    assert res.no_promotion_reason
    assert res.best_config == RetrievalConfig()  # live config unchanged


def test_holdout_rejects_overfit():
    cases = _cases(15)
    ans = {c.question: c.answer for c in cases}
    holdout_qs = {c.question for c in sorted(cases, key=lambda c: c.id)[::5]}

    def hits_for(q, cfg):
        if cfg.semantic_top_k >= 30:
            # candidate helps train but returns NOTHING on holdout (overfit)
            if q in holdout_qs:
                return []
            return [_Hit("e", ans[q])]
        return [_Hit("e", "irrelevant filler")]

    res = optimize_retrieval(_FakeEngine(hits_for), cases, RetrievalConfig(),
                             max_rounds=3, min_confirmed=5)
    assert res.promoted is False
    assert "holdout" in res.no_promotion_reason


def test_evaluate_config_aggregates():
    cases = _cases(4)
    ans = {c.question: c.answer for c in cases}
    eng = _FakeEngine(lambda q, cfg: [_Hit("e", ans[q])])
    agg = evaluate_config(eng, RetrievalConfig(), cases)
    assert agg.n == 4 and agg.mean > 0.5 and agg.zero_retrieval_count == 0


def test_diagnose_fn_invoked_when_deterministic_stalls():
    # A retrieval surface that only single-field neighbors can't reach, but a
    # diagnostician's multi-field suggestion can: requires fusion_mode keyword_only
    # AND keyword_top_k>=8 simultaneously.
    cases = _cases(5)
    ans = {c.question: c.answer for c in cases}

    def hits_for(q, cfg):
        if cfg.fusion_mode == "keyword_only" and cfg.keyword_top_k >= 8:
            return [_Hit("e", ans[q])]
        return [_Hit("e", "irrelevant")]

    called = {"n": 0}

    def diagnose_fn(report):
        called["n"] += 1
        assert "worst_cases" in report and "current_config" in report
        return [{"fusion_mode": "keyword_only", "keyword_top_k": 8}]

    res = optimize_retrieval(_FakeEngine(hits_for), cases, RetrievalConfig(),
                             max_rounds=2, min_confirmed=5, diagnose_fn=diagnose_fn)
    assert called["n"] == 1            # invoked only after deterministic search stalled
    assert res.promoted is True
    assert res.best_config.fusion_mode == "keyword_only"
    assert res.best_config.keyword_top_k == 8


def test_diagnose_fn_not_invoked_when_deterministic_succeeds():
    cases = _cases(5)
    ans = {c.question: c.answer for c in cases}

    def hits_for(q, cfg):
        return [_Hit("e", ans[q])] if cfg.semantic_top_k >= 30 else [_Hit("e", "irrelevant")]

    called = {"n": 0}

    def diagnose_fn(report):
        called["n"] += 1
        return []

    res = optimize_retrieval(_FakeEngine(hits_for), cases, RetrievalConfig(),
                             max_rounds=3, min_confirmed=5, diagnose_fn=diagnose_fn)
    assert res.promoted is True
    assert called["n"] == 0            # deterministic search already promoted


def test_materialize_suggestions_allowlist_and_clamp():
    cur = RetrievalConfig()
    out = materialize_suggestions(cur, [
        {"semantic_top_k": 30},               # valid
        {"unknown_field": 1},                 # rejected (unknown)
        {"max_context": 999},                 # rejected (out of range)
        {"fusion_mode": "keyword_only"},      # valid
        "not a dict",                         # ignored
    ])
    sk = {c.semantic_top_k for c in out}
    modes = {c.fusion_mode for c in out}
    assert 30 in sk
    assert "keyword_only" in modes
    assert all(c.max_context <= 10 for c in out)


# ── retrieval_config_store ─────────────────────────────────────────────

def test_config_store_load_default_when_missing(tmp_path):
    assert RCS.load_retrieval_config(str(tmp_path)) == RetrievalConfig()


def test_config_store_malformed_fails_loud(tmp_path):
    (tmp_path / "retrieval_config.json").write_text("{ not json")
    with pytest.raises(ValueError):
        RCS.load_retrieval_config(str(tmp_path))


def test_config_store_save_promote_and_backup(tmp_path):
    root = str(tmp_path)
    cfg1 = RetrievalConfig(semantic_top_k=10)
    report = {"summary": {"run_id": "run-1"}, "cases": [{"case_id": "c0", "score": 0.5}],
              "history": {"run_id": "run-1", "promoted": True}}
    p = RCS.save_retrieval_config(root, cfg1, run_id="run-1", report=report)
    assert RCS.load_retrieval_config(root) == cfg1
    # run report + history written
    assert (tmp_path / "optimize" / "runs" / "run-1" / "summary.json").exists()
    assert (tmp_path / "optimize" / "runs" / "run-1" / "cases.jsonl").exists()
    assert (tmp_path / "optimize" / "history.jsonl").exists()
    # second promotion backs up the previous config
    cfg2 = RetrievalConfig(semantic_top_k=20)
    RCS.save_retrieval_config(root, cfg2, run_id="run-2",
                              report={"summary": {}, "cases": [], "history": {}})
    assert RCS.load_retrieval_config(root) == cfg2
    assert list(tmp_path.glob("retrieval_config.*.bak"))


def test_config_store_no_promotion_writes_report_only(tmp_path):
    root = str(tmp_path)
    path = RCS.save_retrieval_config(root, None, run_id="run-x",
                                     report={"summary": {}, "cases": [], "history": {}})
    assert "summary.json" in path
    assert not (tmp_path / "retrieval_config.json").exists()  # live config untouched


def test_config_store_rollback(tmp_path):
    root = str(tmp_path)
    RCS.save_retrieval_config(root, RetrievalConfig(semantic_top_k=10), run_id="r1",
                              report={"summary": {}, "cases": [], "history": {}})
    RCS.save_retrieval_config(root, RetrievalConfig(semantic_top_k=20), run_id="r2",
                              report={"summary": {}, "cases": [], "history": {}})
    assert RCS.rollback_retrieval_config(root) is True
    assert RCS.load_retrieval_config(root).semantic_top_k == 10


def test_config_store_rollback_skips_corrupt_backup(tmp_path):
    # A corrupt newest .bak must be skipped, not installed-and-reported-success
    # (review fix: rollback validates before installing).
    root = str(tmp_path)
    RCS.save_retrieval_config(root, RetrievalConfig(semantic_top_k=10), run_id="r1",
                              report={"summary": {}, "cases": [], "history": {}})
    RCS.save_retrieval_config(root, RetrievalConfig(semantic_top_k=20), run_id="r2",
                              report={"summary": {}, "cases": [], "history": {}})
    # plant a corrupt, lexically-newest backup
    (tmp_path / "retrieval_config.99999999T999999Z.zzz.bak").write_text("{ not json")
    assert RCS.rollback_retrieval_config(root) is True
    # rolled back to the valid r1 backup (10), not the corrupt one
    assert RCS.load_retrieval_config(root).semantic_top_k == 10


def test_compute_is_side_effect_free_and_persist_writes(tmp_path, monkeypatch):
    # Cancel-safety (review fix): the worker-thread compute must NOT write the live
    # config; only _persist_result (run post-cancel-check on the loop) writes it.
    import asyncio
    from types import SimpleNamespace
    from nanocode.extensions.memory_evolution import tasks as T

    cases = _cases(5)
    ans = {c.question: c.answer for c in cases}

    def hits_for(q, cfg):
        return [_Hit("e", ans[q])] if cfg.semantic_top_k >= 30 else [_Hit("e", "irrelevant")]

    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "5")
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS", "3")
    monkeypatch.setattr("nanocode.memory.retrieval_eval.cases_from_confirmed", lambda: cases)
    eng = _FakeEngine(hits_for, str(tmp_path))

    outcome = T._compute_optimization(eng, False, SimpleNamespace(), None)
    assert outcome[0] == "result" and outcome[1].promoted is True
    # compute wrote NO live config (cancel-safe)
    assert not (tmp_path / "retrieval_config.json").exists()
    # persist writes it
    summary, _path = T._persist_result(outcome[2], outcome[1])
    assert "promoted" in summary
    assert RCS.load_retrieval_config(str(tmp_path)).semantic_top_k == 30
