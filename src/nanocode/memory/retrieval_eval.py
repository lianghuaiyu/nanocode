"""memory/retrieval_eval.py — deterministic retrieval scoring (docs/22 Phase 3).

Scores a retrieval result for a confirmed QA eval case WITHOUT an LLM judge: a
token-F1 overlap between retrieved memory content and the ground-truth
answer/evidence, plus a small top-rank bonus, minus a zero-retrieval penalty.

    score = 0.55 * max token_f1(hit, answer)
          + 0.35 * max token_f1(hit, evidence_i)
          + 0.10 * top_rank_bonus
          - zero_retrieval_penalty        (only when nothing was retrieved)

This is the optimizer's objective. It is fully deterministic and reproducible —
the promotion gate's correctness comes from the host, not an LLM (docs/22 §4.1).
An optional LLM judge is explicitly out of the first-version promotion path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"\b[\w]+\b", re.UNICODE)
_ANSWER_WEIGHT = 0.55
_EVIDENCE_WEIGHT = 0.35
_TOP_RANK_WEIGHT = 0.10
ZERO_RETRIEVAL_PENALTY = 0.5


@dataclass(frozen=True)
class RetrievalEvalCase:
    id: str
    question: str
    answer: str
    evidence: tuple[str, ...]
    category: str = "general"
    source_ref: str = ""


@dataclass(frozen=True)
class RetrievalEvalResult:
    case_id: str
    score: float
    answer_overlap: float
    evidence_overlap: float
    hit_count: int
    hit_refs: tuple[str, ...]

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall((text or "").lower()) if len(t) > 1}


def token_f1(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    precision = inter / len(ta)
    recall = inter / len(tb)
    return 2 * precision * recall / (precision + recall)


def _hit_text(hit) -> str:
    text = getattr(hit, "lossless_restatement", "") or ""
    kws = getattr(hit, "keywords", None) or []
    return f"{text} {' '.join(kws)}".strip()


def score_case(case: RetrievalEvalCase, hits: list) -> RetrievalEvalResult:
    """Score one case against an ordered list of retrieved entries (rank 0 first)."""
    hit_refs = tuple(getattr(h, "entry_id", "") for h in hits)
    texts = [_hit_text(h) for h in hits]
    if not texts:
        return RetrievalEvalResult(case.id, -ZERO_RETRIEVAL_PENALTY, 0.0, 0.0, 0, ())
    answer_overlap = max(token_f1(t, case.answer) for t in texts)
    evidence_overlap = 0.0
    if case.evidence:
        evidence_overlap = max(token_f1(t, ev) for t in texts for ev in case.evidence)
    top_rank_bonus = token_f1(texts[0], case.answer)
    score = (_ANSWER_WEIGHT * answer_overlap
             + _EVIDENCE_WEIGHT * evidence_overlap
             + _TOP_RANK_WEIGHT * top_rank_bonus)
    return RetrievalEvalResult(case.id, score, answer_overlap, evidence_overlap,
                               len(texts), hit_refs)


def cases_from_confirmed() -> list[RetrievalEvalCase]:
    """Build eval cases from confirmed QA candidates.

    Candidates with an empty answer or no non-blank evidence are excluded — they
    cannot be scored deterministically (docs/22 §3 rule 3)."""
    from .eval_store import list_confirmed
    out: list[RetrievalEvalCase] = []
    for c in list_confirmed():
        if not (c.answer or "").strip():
            continue
        evidence = tuple(e for e in (c.evidence or []) if (e or "").strip())
        if not evidence:
            continue
        out.append(RetrievalEvalCase(
            id=c.id, question=c.question, answer=c.answer, evidence=evidence,
            category=c.category or "general",
            source_ref=((c.source or {}).get("memory_ref") or "")))
    return out
