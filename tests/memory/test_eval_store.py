"""Tests for the memory eval store: validation, id/dedup, CRUD, prune compatibility.

The eval store uses per-file `<id>.json` under `eval/<status>/` subdirectories
(pending/confirmed/rejected). It must NOT break maintenance.prune_orphaned_evals,
which globs only the root `eval/` directory (non-recursive).

All tests run against the isolated NANOCODE_HOME (conftest autouse fixture) with
real file I/O. Provenance validation requires a session to exist, so we create one
via session.v2.write_state.
"""
import json

import pytest

from nanocode.memory import eval_store
from nanocode.session import v2


def _mk_session(sid: str = "sess-1") -> str:
    """Create a v2 session so provenance validation passes."""
    v2.write_state(sid, {"id": sid, "startTime": "2026-06-06T00:00:00Z"})
    return sid


def _candidate(**overrides) -> eval_store.MemoryEvalCandidate:
    sid = overrides.pop("session_id", None)
    if sid is None:
        sid = _mk_session()
    base = dict(
        question="What is the project goal?",
        answer="Ship v2 by Q1.",
        source={"session_id": sid, "memory_ref": "project_goals.md", "observation_ref": ""},
        evidence=["We want to ship v2 by end of Q1."],
    )
    base.update(overrides)
    return eval_store.MemoryEvalCandidate(**base)


# ─── Validation ──────────────────────────────────────────────


class TestValidate:
    def test_valid_candidate_passes(self):
        c = _candidate()
        # Should not raise
        eval_store.validate_candidate(c)

    def test_empty_question_raises(self):
        c = _candidate(question="   ")
        with pytest.raises(ValueError, match="question"):
            eval_store.validate_candidate(c)

    def test_empty_answer_raises(self):
        c = _candidate(answer="")
        with pytest.raises(ValueError, match="answer"):
            eval_store.validate_candidate(c)

    def test_missing_session_id_raises(self):
        c = _candidate()
        c.source["session_id"] = ""
        with pytest.raises(ValueError, match="session"):
            eval_store.validate_candidate(c)

    def test_nonexistent_session_raises(self):
        c = _candidate()
        c.source["session_id"] = "does-not-exist"
        with pytest.raises(ValueError, match="session"):
            eval_store.validate_candidate(c)

    def test_no_memory_or_observation_ref_raises(self):
        c = _candidate()
        c.source["memory_ref"] = ""
        c.source["observation_ref"] = ""
        with pytest.raises(ValueError, match="memory_ref|observation_ref"):
            eval_store.validate_candidate(c)

    def test_observation_ref_alone_is_ok(self):
        c = _candidate()
        c.source["memory_ref"] = ""
        c.source["observation_ref"] = "obs-123"
        eval_store.validate_candidate(c)

    def test_empty_evidence_raises(self):
        c = _candidate(evidence=[])
        with pytest.raises(ValueError, match="evidence"):
            eval_store.validate_candidate(c)

    def test_all_whitespace_evidence_raises(self):
        c = _candidate(evidence=["   ", "\t", ""])
        with pytest.raises(ValueError, match="evidence"):
            eval_store.validate_candidate(c)


# ─── Id + dedup key ──────────────────────────────────────────


class TestIdAndDedupKey:
    def test_dedup_key_normalizes_case_and_whitespace(self):
        k1 = eval_store._dedup_key("What  is   X?", "Answer Y")
        k2 = eval_store._dedup_key("what is x?", "answer  y")
        assert k1 == k2

    def test_dedup_key_separates_question_and_answer(self):
        # Same concatenation but different q/a split must differ.
        k1 = eval_store._dedup_key("ab", "c")
        k2 = eval_store._dedup_key("a", "bc")
        assert k1 != k2

    def test_dedup_key_question_sensitive(self):
        k1 = eval_store._dedup_key("q1", "a")
        k2 = eval_store._dedup_key("q2", "a")
        assert k1 != k2

    def test_dedup_key_answer_sensitive(self):
        k1 = eval_store._dedup_key("q", "a1")
        k2 = eval_store._dedup_key("q", "a2")
        assert k1 != k2

    def test_candidate_id_is_16_hex(self):
        cid = eval_store._candidate_id("some question", "some answer")
        assert len(cid) == 16
        assert all(ch in "0123456789abcdef" for ch in cid)

    def test_candidate_id_matches_dedup_key(self):
        # Same dedup key (after normalization) -> same id.
        a = eval_store._candidate_id("What  IS x?", "ANSWER")
        b = eval_store._candidate_id("what is x?", "answer")
        assert a == b


# ─── CRUD ────────────────────────────────────────────────────


class TestCRUD:
    def test_add_pending_writes_file_and_fills_fields(self):
        c = _candidate()
        cid = eval_store.add_pending(c)
        assert cid == eval_store._candidate_id(c.question, c.answer)

        path = eval_store._status_dir("pending") / f"{cid}.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["id"] == cid
        assert data["status"] == "pending"
        assert data["created_at"]  # populated
        # source_memory = basename of memory_ref
        assert data["source_memory"] == "project_goals.md"

    def test_add_pending_source_memory_basename(self):
        c = _candidate()
        c.source["memory_ref"] = "sub/dir/notes.md"
        cid = eval_store.add_pending(c)
        got = eval_store.get_candidate(cid)
        assert got.source_memory == "notes.md"

    def test_add_pending_validation_failure_raises(self):
        c = _candidate(question="")
        with pytest.raises(ValueError):
            eval_store.add_pending(c)

    def test_add_pending_idempotent_same_pair(self):
        c1 = _candidate()
        id1 = eval_store.add_pending(c1)
        # Re-add same q/a (different evidence/confidence) -> same id, no second file
        c2 = _candidate(confidence=0.9, evidence=["different evidence"])
        id2 = eval_store.add_pending(c2)
        assert id1 == id2
        files = list(eval_store._status_dir("pending").glob("*.json"))
        assert len(files) == 1

    def test_add_pending_does_not_resurrect_rejected(self):
        c = _candidate()
        cid = eval_store.add_pending(c)
        assert eval_store.reject(cid) is True
        # Same q/a re-added -> stays rejected, no new pending file
        again = _candidate()
        cid2 = eval_store.add_pending(again)
        assert cid2 == cid
        assert eval_store.list_pending() == []
        assert len(eval_store.list_rejected()) == 1

    def test_add_pending_does_not_duplicate_confirmed(self):
        c = _candidate()
        cid = eval_store.add_pending(c)
        assert eval_store.confirm(cid) is True
        again = _candidate()
        cid2 = eval_store.add_pending(again)
        assert cid2 == cid
        assert eval_store.list_pending() == []
        assert len(eval_store.list_confirmed()) == 1

    def test_list_empty(self):
        assert eval_store.list_pending() == []
        assert eval_store.list_confirmed() == []
        assert eval_store.list_rejected() == []

    def test_confirm_moves_to_confirmed(self):
        cid = eval_store.add_pending(_candidate())
        assert eval_store.confirm(cid) is True
        assert eval_store.list_pending() == []
        confirmed = eval_store.list_confirmed()
        assert len(confirmed) == 1
        assert confirmed[0].id == cid
        assert confirmed[0].status == "confirmed"
        # File physically moved
        assert not (eval_store._status_dir("pending") / f"{cid}.json").exists()
        assert (eval_store._status_dir("confirmed") / f"{cid}.json").exists()

    def test_reject_moves_to_rejected(self):
        cid = eval_store.add_pending(_candidate())
        assert eval_store.reject(cid) is True
        assert eval_store.list_pending() == []
        rejected = eval_store.list_rejected()
        assert len(rejected) == 1
        assert rejected[0].status == "rejected"

    def test_confirm_unknown_returns_false(self):
        assert eval_store.confirm("deadbeefdeadbeef") is False

    def test_confirm_already_confirmed_returns_false(self):
        cid = eval_store.add_pending(_candidate())
        assert eval_store.confirm(cid) is True
        # Already confirmed -> confirm/reject only act on pending
        assert eval_store.confirm(cid) is False
        assert eval_store.reject(cid) is False

    def test_reject_already_rejected_returns_false(self):
        cid = eval_store.add_pending(_candidate())
        assert eval_store.reject(cid) is True
        assert eval_store.reject(cid) is False

    def test_get_candidate_across_states(self):
        cid = eval_store.add_pending(_candidate())
        assert eval_store.get_candidate(cid).status == "pending"
        eval_store.confirm(cid)
        assert eval_store.get_candidate(cid).status == "confirmed"

    def test_get_candidate_unknown_returns_none(self):
        assert eval_store.get_candidate("0000000000000000") is None

    def test_confirmed_dev_questions_returns_tuples(self):
        sid = _mk_session()
        c1 = _candidate(session_id=sid, question="Q1?", answer="A1")
        c2 = _candidate(session_id=sid, question="Q2?", answer="A2")
        eval_store.confirm(eval_store.add_pending(c1))
        # c2 stays pending
        eval_store.add_pending(c2)
        pairs = eval_store.confirmed_dev_questions()
        assert ("Q1?", "A1") in pairs
        assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs)
        # Only confirmed contribute
        assert ("Q2?", "A2") not in pairs


# ─── Prune Compatibility ─────────────────────────────────────


class TestPruneCompat:
    def test_prune_default_root_does_not_touch_subdir_files(self):
        """maintenance.prune_orphaned_evals() (default root glob, non-recursive)
        must not delete eval store files living in eval/<status>/ subdirectories,
        even when their source_memory no longer exists on disk."""
        from nanocode.memory import maintenance

        # source_memory 'project_goals.md' does NOT exist in the project memory dir
        cid = eval_store.add_pending(_candidate())
        eval_store.confirm(cid)
        confirmed_path = eval_store._status_dir("confirmed") / f"{cid}.json"
        assert confirmed_path.exists()

        # Default prune globs only the eval/ root (non-recursive) -> 0 pruned.
        pruned = maintenance.prune_orphaned_evals()
        assert pruned == 0
        assert confirmed_path.exists()

    def test_prune_confirmed_subdir_explicitly_uses_source_memory(self):
        """Sub-stage C calls prune_orphaned_evals(eval/confirmed). Confirmed files
        carry top-level source_memory so the orphan check works there."""
        from nanocode.memory import maintenance

        cid = eval_store.add_pending(_candidate())  # source_memory project_goals.md (missing)
        eval_store.confirm(cid)
        confirmed_dir = eval_store._status_dir("confirmed")

        pruned = maintenance.prune_orphaned_evals(confirmed_dir)
        assert pruned == 1
        assert not (confirmed_dir / f"{cid}.json").exists()


# ─── Package-level exports ───────────────────────────────────


class TestExports:
    def test_public_names_exported_from_memory_package(self):
        import nanocode.memory as mem

        names = [
            "MemoryEvalCandidate", "add_pending", "list_pending", "list_confirmed",
            "list_rejected", "get_candidate", "confirm", "reject",
            "confirmed_dev_questions",
        ]
        for n in names:
            assert hasattr(mem, n), f"nanocode.memory should export {n}"

    def test_exported_add_pending_is_eval_store_function(self):
        from nanocode.memory import add_pending as exported

        assert exported is eval_store.add_pending
