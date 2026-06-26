"""Pending steer/follow-up queue for child-session subagents."""

from __future__ import annotations

import json
from uuid import uuid4
from typing import Any

from ..agent.events import UserMessageAccepted
from ..runs.models import TERMINAL_RUN_STATUSES
from ..session import tree
from . import run_record


DELIVERIES = frozenset({"steer", "follow_up"})


def _now() -> str:
    return tree.now_iso()


def _append(child_session_id: str, record: dict[str, Any]) -> None:
    path = run_record.pending_steer_path(child_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _latest_records(child_session_id: str) -> list[dict[str, Any]]:
    path = run_record.pending_steer_path(child_session_id)
    if not path.exists():
        return []
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        rid = item.get("id")
        if not rid:
            continue
        if rid not in latest:
            order.append(rid)
        latest[rid] = item
    return [latest[rid] for rid in order]


def queued_count(child_session_id: str) -> int:
    return sum(1 for item in _latest_records(child_session_id) if item.get("state") == "queued")


def queue_steer(child_session_id: str, prompt: str, *, delivery: str = "steer") -> dict[str, Any]:
    if delivery not in DELIVERIES:
        raise ValueError(f"invalid delivery: {delivery}")
    status = run_record.read_status(child_session_id)
    if status.get("status") in TERMINAL_RUN_STATUSES:
        raise RuntimeError(f"run {child_session_id} is terminal ({status.get('status')}); use resume")
    record = {
        "id": f"steer_{uuid4().hex[:12]}",
        "delivery": delivery,
        "prompt": prompt,
        "queuedAt": _now(),
        "state": "queued",
    }
    _append(child_session_id, record)
    count = queued_count(child_session_id)
    run_record.update_status(child_session_id, pendingSteerCount=count)
    run_record.append_event(child_session_id, "steer_queued", steerId=record["id"],
                            delivery=delivery)
    return record


def drain_pending_steers(agent, *, delivery: str) -> int:
    """Apply queued steer/follow-up records to a live child agent.

    The actual conversation entry is written through ``AgentSession.record_event``
    by emitting ``UserMessageAccepted`` on the child agent.
    """
    if delivery not in DELIVERIES:
        raise ValueError(f"invalid delivery: {delivery}")
    child_session_id = getattr(agent, "_tree_session_id", None)
    if not child_session_id:
        return 0
    applied = 0
    for item in _latest_records(child_session_id):
        if item.get("state") != "queued" or item.get("delivery") != delivery:
            continue
        prompt = item.get("prompt") or ""
        agent.emit(UserMessageAccepted(text=prompt))
        entry_id = None
        try:
            entry_id = agent._session_mgr.last_user_message_id()
        except Exception:
            entry_id = None
        update = dict(item)
        update.update({"state": "applied", "appliedAt": _now(), "entryId": entry_id})
        _append(child_session_id, update)
        run_record.append_event(child_session_id, "steer_applied",
                                steerId=item["id"], delivery=delivery, entryId=entry_id)
        applied += 1
    if applied:
        run_record.update_status(child_session_id, pendingSteerCount=queued_count(child_session_id))
    return applied
