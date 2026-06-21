"""Replay child-owned subagent run records.

Discovery starts from canonical session headers via ``children(parent_id)`` and
then reads each child's ``subagent-run/`` sidecar. The ledger never scans
sidecars to invent child sessions.
"""

from __future__ import annotations

from typing import Any

from ..session import tree
from ..session.manager import SessionManager, children
from ..subagents import run_record
from .models import AgentRunRecord, TERMINAL_RUN_STATUSES


class RunLedger:
    """Read/write facade for durable child-session run records."""

    def replay(self, child_session_id: str) -> AgentRunRecord:
        if not SessionManager.exists(child_session_id):
            raise FileNotFoundError(f"child session does not exist: {child_session_id}")
        status = run_record.read_status(child_session_id)
        result = run_record.read_result(child_session_id)
        summary = _summary(result)
        return AgentRunRecord.from_status(status, summary=summary)

    def list_for_parent(self, parent_session_id: str, *, status: str | None = None) -> list[AgentRunRecord]:
        records: list[AgentRunRecord] = []
        for child_id in children(parent_session_id):
            try:
                rec = self.replay(child_id)
            except FileNotFoundError:
                continue
            if status is None or rec.status == status:
                records.append(rec)
        return records

    def reconcile_for_parent(
        self,
        parent_session_id: str,
        *,
        live_run_ids: set[str] | frozenset[str] | None = None,
    ) -> list[AgentRunRecord]:
        live = set(live_run_ids or set())
        records: list[AgentRunRecord] = []
        for child_id in children(parent_session_id):
            try:
                rec = self.replay(child_id)
            except FileNotFoundError:
                continue
            if rec.status not in TERMINAL_RUN_STATUSES and child_id not in live:
                rec = self.mark_lost(child_id, reason="no live coroutine during rebind")
            records.append(rec)
        return records

    def mark_lost(self, child_session_id: str, *, reason: str) -> AgentRunRecord:
        rec = self.replay(child_session_id)
        if rec.status in TERMINAL_RUN_STATUSES:
            return rec
        self.update_status(
            child_session_id,
            status="lost",
            endedAt=tree.now_iso(),
            error=reason,
        )
        run_record.append_event(child_session_id, "lost", reason=reason)
        return self.replay(child_session_id)

    def result(
        self,
        child_session_id: str,
        *,
        include_events: bool = False,
        tail_events: int = 20,
    ) -> dict[str, Any]:
        rec = self.replay(child_session_id)
        result = run_record.read_result(child_session_id)
        out: dict[str, Any] = {
            "childSessionId": rec.child_session_id,
            "runId": rec.run_id,
            "status": rec.status,
            "summary": rec.summary or "",
            "result": result,
            "resultPath": rec.result_path,
            "resultEntryId": rec.result_entry_id,
            "promptEntryId": rec.prompt_entry_id,
            "worktreePath": rec.worktree_path,
            "error": rec.error,
            "pendingSteerCount": rec.pending_steer_count,
        }
        if include_events:
            events = run_record.read_events(child_session_id)
            out["eventsTail"] = events[-max(0, int(tail_events)):]
        return out

    def append_event(self, child_session_id: str, event_type: str, **data: Any) -> dict[str, Any]:
        if not SessionManager.exists(child_session_id):
            raise FileNotFoundError(f"child session does not exist: {child_session_id}")
        return run_record.append_event(child_session_id, event_type, **data)

    def update_status(self, child_session_id: str, **fields: Any) -> dict[str, Any]:
        if not SessionManager.exists(child_session_id):
            raise FileNotFoundError(f"child session does not exist: {child_session_id}")
        return run_record.update_status(child_session_id, **fields)


def _summary(result: str, limit: int = 300) -> str:
    text = " ".join((result or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
