"""Memory eval store: pending/confirmed/rejected QA candidates for EvolveMem.

Layout (per-file `<id>.json` under status subdirectories):

    <simplemem>/eval/
        pending/<id>.json
        confirmed/<id>.json
        rejected/<id>.json

This layout is deliberate. `maintenance.prune_orphaned_evals` globs only the
ROOT `eval/` directory (non-recursive), so files in `eval/<status>/` are never
touched by it — confirmation flow and pruning stay decoupled. When sub-stage C
needs to prune confirmed evals, it calls
`prune_orphaned_evals(eval_dir=eval/confirmed)` explicitly, and that works
because confirmed files carry a top-level redundant `source_memory` field
(= basename of `source.memory_ref`).

Design constraints (spec §14):
- Confirmation is a HUMAN action via the REPL only. The memory TOOL schema gets
  no eval action — the model can neither add nor confirm candidates (anti-self-
  validation).
- `add_pending` is idempotent across all three states (a question/answer pair
  that already exists in pending/confirmed/rejected is not re-written; rejected
  candidates are NOT resurrected).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .maintenance import _simplemem_dir
from ..session import v2
from ..session.store import load_session


# ─── Data Model ──────────────────────────────────────────────


@dataclass
class MemoryEvalCandidate:
    """A single QA eval candidate with provenance.

    `source` carries provenance: {session_id, memory_ref, observation_ref}.
    `id`, `created_at`, `source_memory` are populated by `add_pending`.
    """
    question: str
    answer: str
    source: dict
    evidence: list[str] = field(default_factory=list)
    category: str = "general"
    confidence: float = 0.0
    status: str = "pending"
    id: str = ""
    created_at: str = ""
    source_memory: str = ""  # top-level redundant copy of basename(source.memory_ref)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEvalCandidate":
        fields = {
            "question", "answer", "source", "evidence", "category",
            "confidence", "status", "id", "created_at", "source_memory",
        }
        return cls(**{k: v for k, v in data.items() if k in fields})


# ─── Directory Layout ────────────────────────────────────────

VALID_STATUSES = ("pending", "confirmed", "rejected")


def _eval_root() -> Path:
    d = _simplemem_dir() / "eval"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _status_dir(status: str) -> Path:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status!r}; must be one of {VALID_STATUSES}")
    d = _eval_root() / status
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Normalization / Id / Dedup ──────────────────────────────


def _norm(text: str) -> str:
    """Lowercase + fold whitespace runs to a single space, stripped."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _dedup_key(question: str, answer: str) -> str:
    """Dedup key = norm(question) + 0x1f + norm(answer).

    The 0x1f separator keeps the question/answer boundary unambiguous so that
    ("ab","c") and ("a","bc") map to distinct keys.
    """
    return _norm(question) + "\x1f" + _norm(answer)


def _candidate_id(question: str, answer: str) -> str:
    return hashlib.sha256(_dedup_key(question, answer).encode("utf-8")).hexdigest()[:16]


def _session_exists(session_id: str) -> bool:
    if not session_id:
        return False
    if v2.is_v2_session(session_id):
        return True
    return load_session(session_id) is not None


# ─── Validation ──────────────────────────────────────────────


def validate_candidate(c: MemoryEvalCandidate) -> None:
    """Raise ValueError if the candidate is not eligible for the eval store.

    Rules:
    - question/answer non-empty (after strip)
    - source.session_id exists (v2 session or loadable flat session)
    - at least one of source.memory_ref / source.observation_ref non-empty
    - at least one non-blank evidence entry
    """
    if not (c.question or "").strip():
        raise ValueError("question must be non-empty")
    if not (c.answer or "").strip():
        raise ValueError("answer must be non-empty")

    source = c.source or {}
    session_id = (source.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("source.session_id is required")
    if not _session_exists(session_id):
        raise ValueError(f"source.session_id {session_id!r} references a session that does not exist")

    memory_ref = (source.get("memory_ref") or "").strip()
    observation_ref = (source.get("observation_ref") or "").strip()
    if not (memory_ref or observation_ref):
        raise ValueError("at least one of source.memory_ref or source.observation_ref is required")

    if not any((e or "").strip() for e in (c.evidence or [])):
        raise ValueError("at least one non-blank evidence entry is required")


# ─── CRUD ────────────────────────────────────────────────────


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _find_path(cid: str) -> Path | None:
    """Return the file path of a candidate across all three states, or None."""
    for status in VALID_STATUSES:
        p = _status_dir(status) / f"{cid}.json"
        if p.exists():
            return p
    return None


def add_pending(c: MemoryEvalCandidate) -> str:
    """Validate + dedup-across-states + write a pending candidate.

    Idempotent: if the same question/answer pair already exists in ANY state
    (pending/confirmed/rejected), returns its id without rewriting. Rejected
    candidates are therefore never resurrected.
    """
    validate_candidate(c)
    cid = _candidate_id(c.question, c.answer)

    # Cross-state dedup: never resurrect or duplicate.
    existing = _find_path(cid)
    if existing is not None:
        return cid

    c.id = cid
    c.status = "pending"
    if not c.created_at:
        c.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    memory_ref = ((c.source or {}).get("memory_ref") or "").strip()
    c.source_memory = Path(memory_ref).name if memory_ref else ""

    _atomic_write(_status_dir("pending") / f"{cid}.json", c.to_dict())
    return cid


def _load_dir(status: str) -> list[MemoryEvalCandidate]:
    out: list[MemoryEvalCandidate] = []
    for f in sorted(_status_dir(status).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(MemoryEvalCandidate.from_dict(data))
    return out


def list_pending() -> list[MemoryEvalCandidate]:
    return _load_dir("pending")


def list_confirmed() -> list[MemoryEvalCandidate]:
    return _load_dir("confirmed")


def list_rejected() -> list[MemoryEvalCandidate]:
    return _load_dir("rejected")


def get_candidate(cid: str) -> MemoryEvalCandidate | None:
    p = _find_path(cid)
    if p is None:
        return None
    try:
        return MemoryEvalCandidate.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return None


def _transition(cid: str, new_status: str) -> bool:
    """Move a PENDING candidate to new_status. Returns False if not pending.

    Write target first, then delete source — a crash mid-transition leaves a
    duplicate that cross-state dedup in add_pending tolerates.
    """
    src = _status_dir("pending") / f"{cid}.json"
    if not src.exists():
        return False
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    data["status"] = new_status
    _atomic_write(_status_dir(new_status) / f"{cid}.json", data)
    src.unlink()
    return True


def confirm(cid: str) -> bool:
    """Confirm a pending candidate (human action). False if not pending."""
    return _transition(cid, "confirmed")


def reject(cid: str) -> bool:
    """Reject a pending candidate (human action). False if not pending."""
    return _transition(cid, "rejected")


def confirmed_dev_questions() -> list[tuple[str, str]]:
    """Confirmed (question, answer) pairs for EvolveMem dev-question optimization."""
    return [(c.question, c.answer) for c in list_confirmed()]
