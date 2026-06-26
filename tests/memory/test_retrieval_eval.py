"""docs/22 Phase 3: deterministic retrieval eval scorer."""
from dataclasses import dataclass

from nanocode.memory import retrieval_eval as RE
from nanocode.memory.retrieval_eval import (
    RetrievalEvalCase, score_case, token_f1, cases_from_confirmed,
)


@dataclass
class _Hit:
    entry_id: str
    lossless_restatement: str
    keywords: tuple = ()


def test_token_f1_basic():
    assert token_f1("the quick brown fox", "quick brown") > 0.5
    assert token_f1("alpha beta", "gamma delta") == 0.0
    assert token_f1("", "x") == 0.0


def test_score_case_strong_match_high():
    case = RetrievalEvalCase(id="c1", question="when ship v2?",
                             answer="by end of Q1",
                             evidence=("ship v2 by end of Q1",))
    hits = [_Hit("e1", "We will ship v2 by end of Q1.")]
    r = score_case(case, hits)
    assert r.score > 0.5
    assert r.answer_overlap > 0 and r.evidence_overlap > 0
    assert r.hit_count == 1 and r.hit_refs == ("e1",)


def test_score_case_zero_retrieval_penalized():
    case = RetrievalEvalCase(id="c2", question="q", answer="a", evidence=("a",))
    r = score_case(case, [])
    assert r.score == -RE.ZERO_RETRIEVAL_PENALTY
    assert r.hit_count == 0


def test_score_case_top_rank_bonus_orders():
    case = RetrievalEvalCase(id="c3", question="capital of france",
                             answer="Paris is the capital of France",
                             evidence=("Paris is the capital of France",))
    good_first = [_Hit("e1", "Paris is the capital of France"),
                  _Hit("e2", "unrelated content about cats")]
    good_second = [_Hit("e2", "unrelated content about cats"),
                   _Hit("e1", "Paris is the capital of France")]
    assert score_case(case, good_first).score > score_case(case, good_second).score


def test_cases_from_confirmed_excludes_empty(monkeypatch):
    @dataclass
    class _C:
        id: str
        question: str
        answer: str
        evidence: list
        category: str = "general"
        source: dict = None

    confirmed = [
        _C("ok", "q1", "answer one", ["evidence one"], source={"memory_ref": "m1"}),
        _C("no_answer", "q2", "  ", ["e"], source={}),
        _C("no_evidence", "q3", "a", [" "], source={}),
    ]
    monkeypatch.setattr("nanocode.memory.eval_store.list_confirmed", lambda: confirmed)
    cases = cases_from_confirmed()
    assert [c.id for c in cases] == ["ok"]
    assert cases[0].source_ref == "m1"
