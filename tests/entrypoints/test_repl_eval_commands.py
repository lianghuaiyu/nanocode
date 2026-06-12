"""Tests for the REPL `/memory eval` command handler (builtin.handle_eval_command).

handle_eval_command(rest) is a pure function returning a string, so it can be
tested without a running REPL. It is the single human entry point for the eval
confirm flow (pending|confirmed|rejected | confirm <id> | reject <id>).
"""
import pytest

from nanocode.entrypoints.commands import builtin
from nanocode.memory import eval_store
from nanocode.session import v2


def _mk_session(sid: str = "sess-1") -> str:
    v2.write_state(sid, {"id": sid, "startTime": "2026-06-06T00:00:00Z"})
    return sid


def _candidate(question="What is the goal?", answer="Ship v2.", **ov):
    sid = ov.pop("session_id", None) or _mk_session()
    return eval_store.MemoryEvalCandidate(
        question=question,
        answer=answer,
        source={"session_id": sid, "memory_ref": "project_goals.md", "observation_ref": ""},
        evidence=["evidence line"],
        **ov,
    )


class TestHandleEvalCommand:
    def test_pending_empty(self):
        out = builtin.handle_eval_command("pending")
        assert "pending" in out.lower()
        # Should signal emptiness, not crash.
        assert "no" in out.lower() or "0" in out

    def test_empty_rest_defaults_to_pending(self):
        # `/memory eval` with no subcommand lists pending.
        out = builtin.handle_eval_command("")
        assert "pending" in out.lower()

    def test_pending_list_shows_candidates(self):
        cid = eval_store.add_pending(_candidate(question="Q-pending?", answer="A-pending"))
        out = builtin.handle_eval_command("pending")
        assert cid in out
        assert "Q-pending?" in out

    def test_confirm_moves_candidate(self):
        cid = eval_store.add_pending(_candidate())
        out = builtin.handle_eval_command(f"confirm {cid}")
        assert cid in out
        assert "confirm" in out.lower()
        # State actually changed
        assert eval_store.get_candidate(cid).status == "confirmed"

    def test_reject_moves_candidate(self):
        cid = eval_store.add_pending(_candidate())
        out = builtin.handle_eval_command(f"reject {cid}")
        assert cid in out
        assert "reject" in out.lower()
        assert eval_store.get_candidate(cid).status == "rejected"

    def test_confirm_unknown_id_reports_failure(self):
        out = builtin.handle_eval_command("confirm 0000000000000000")
        low = out.lower()
        assert "not" in low or "no pending" in low or "fail" in low

    def test_reject_unknown_id_reports_failure(self):
        out = builtin.handle_eval_command("reject 0000000000000000")
        low = out.lower()
        assert "not" in low or "no pending" in low or "fail" in low

    def test_confirm_without_id_shows_usage(self):
        out = builtin.handle_eval_command("confirm")
        assert "usage" in out.lower()

    def test_reject_without_id_shows_usage(self):
        out = builtin.handle_eval_command("reject")
        assert "usage" in out.lower()

    def test_confirmed_list(self):
        cid = eval_store.add_pending(_candidate(question="Q-conf?", answer="A-conf"))
        eval_store.confirm(cid)
        out = builtin.handle_eval_command("confirmed")
        assert cid in out
        assert "Q-conf?" in out

    def test_rejected_list(self):
        cid = eval_store.add_pending(_candidate(question="Q-rej?", answer="A-rej"))
        eval_store.reject(cid)
        out = builtin.handle_eval_command("rejected")
        assert cid in out
        assert "Q-rej?" in out

    def test_unknown_subcommand_shows_usage(self):
        out = builtin.handle_eval_command("frobnicate")
        assert "usage" in out.lower()


class TestFmtEvalRow:
    def test_fmt_row_includes_id_and_question(self):
        c = _candidate(question="Some question?", answer="Some answer")
        c.id = "abc123abc123abc1"
        row = builtin._fmt_eval_row(c)
        assert "abc123abc123abc1" in row
        assert "Some question?" in row
