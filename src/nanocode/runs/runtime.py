"""AgentRunRuntime host projection.

This class intentionally stores no durable truth. It wraps ``RunLedger`` for
query/control operations and can be rebound from canonical session headers.
"""

from __future__ import annotations

from .ledger import RunLedger
from .models import AgentRunRecord, TERMINAL_RUN_STATUSES
from ..subagents.steer import queue_steer


class AgentRunRuntime:
    def __init__(self, ledger: RunLedger | None = None) -> None:
        self.ledger = ledger or RunLedger()

    def rebind(
        self,
        parent_session_id: str,
        *,
        live_run_ids: set[str] | frozenset[str] | None = None,
    ) -> list[AgentRunRecord]:
        return self.ledger.reconcile_for_parent(parent_session_id, live_run_ids=live_run_ids)

    def list(
        self,
        parent_session_id: str,
        *,
        status: str | None = None,
        live_run_ids: set[str] | frozenset[str] | None = None,
    ) -> list[AgentRunRecord]:
        # docs/25 A4a：重绘/列举走只读视图（不 mark_lost、不写放大）。持久化 reconcile 经
        # 显式 rebind() / per-child _reconcile_run / run_cancel 触发。
        records = self.ledger.view_for_parent(parent_session_id, live_run_ids=live_run_ids)
        if status is not None:
            records = [r for r in records if r.status == status]
        return records

    def status(self, child_session_id: str) -> AgentRunRecord:
        return self.ledger.replay(child_session_id)

    def output(self, child_session_id: str, *, include_events: bool = False,
               tail_events: int = 20) -> dict:
        return self.ledger.result(child_session_id, include_events=include_events,
                                  tail_events=tail_events)

    def mark_lost(self, child_session_id: str, *, reason: str) -> AgentRunRecord:
        return self.ledger.mark_lost(child_session_id, reason=reason)

    def send(self, child_session_id: str, prompt: str, *, delivery: str = "steer") -> dict:
        rec = self.ledger.replay(child_session_id)
        if rec.status in TERMINAL_RUN_STATUSES:
            raise RuntimeError(f"run {child_session_id} is terminal ({rec.status}); use resume")
        return queue_steer(child_session_id, prompt, delivery=delivery)
