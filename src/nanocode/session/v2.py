"""Session v2 derived state, task envelopes, and host task directories.

Conversation history is authoritative in ``session.jsonl``. Sub-agent durable
transcript authority lives in each child ``session.jsonl``; parent-visible
task state is a bounded ``task_event`` envelope in the parent session. The
child-owned ``subagent-run/`` sidecar remains an operational projection/cache.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..paths import sessions_dir
from . import tree


TASK_EVENT_SCHEMA_VERSION = 1
TASK_SUMMARY_LIMIT = 1200


def session_root(session_id: str) -> Path:
    return sessions_dir() / session_id


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def is_v2_session(session_id: str) -> bool:
    return (session_root(session_id) / "state.json").exists()


def write_state(session_id: str, state: dict) -> None:
    _write_json(session_root(session_id) / "state.json", state)


def read_state(session_id: str) -> dict | None:
    p = session_root(session_id) / "state.json"
    return _read_json(p, None) if p.exists() else None


def task_dir(session_id: str, task_id: str) -> Path:
    d = session_root(session_id) / "tasks" / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_task_id() -> str:
    """Mint a parent/model-visible logical task handle.

    This is intentionally above the child transcript identity. ``run_id`` remains
    equal to ``child_session_id``; retry/attempt semantics can reuse the same
    ``task_id`` while creating a new child session.
    """
    return tree.new_id("task")


def _compact_text(value: Any, limit: int = TASK_SUMMARY_LIMIT) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def append_task_envelope(
    manager,
    event: str,
    *,
    task_id: str,
    child_session_id: str,
    run_id: str | None = None,
    agent_id: str | None = None,
    spawn_entry_id: str | None = None,
    agent_type: str | None = None,
    description: str | None = None,
    status: str | None = None,
    background: bool | None = None,
    context_mode: str | None = None,
    isolation: str | None = None,
    group_id: str | None = None,
    model: dict[str, Any] | None = None,
    worktree_path: str | None = None,
    result_summary: str | None = None,
    result_path: str | None = None,
    error: str | None = None,
    pending_approval: dict[str, Any] | None = None,
    delivery: str | None = None,
    action: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> object:
    """Append a bounded parent-visible task envelope to ``session.jsonl``.

    The envelope deliberately contains status/result metadata only. It never
    includes the child transcript or raw result body, so parent context remains
    bounded while replay can still recover child run state when the sidecar is
    missing.
    """
    data: dict[str, Any] = {
        "schemaVersion": TASK_EVENT_SCHEMA_VERSION,
        "event": event,
        "taskId": task_id,
        "runId": run_id or child_session_id,
        "childSessionId": child_session_id,
    }
    optional = {
        "agentId": agent_id,
        "spawnEntryId": spawn_entry_id,
        "agentType": agent_type,
        "description": description,
        "status": status,
        "background": background,
        "contextMode": context_mode,
        "isolation": isolation,
        "groupId": group_id,
        "model": model,
        "worktreePath": worktree_path,
        "resultSummary": _compact_text(result_summary),
        "resultPath": result_path,
        "error": _compact_text(error),
        "pendingApproval": pending_approval,
        "delivery": delivery,
        "action": action,
        "metrics": metrics,
    }
    data.update({k: v for k, v in optional.items() if v is not None})
    return manager.append(tree.TASK_EVENT, data)


def replay_task_envelopes(entries: list[tree.Entry]) -> dict[str, dict[str, Any]]:
    """Fold parent ``task_event`` entries into one bounded projection per child."""
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.type != tree.TASK_EVENT:
            continue
        data = dict(entry.data or {})
        child_id = data.get("childSessionId")
        if not child_id:
            continue
        task_id = data.get("taskId") or child_id
        rec = out.setdefault(child_id, {
            "schemaVersion": TASK_EVENT_SCHEMA_VERSION,
            "taskId": task_id,
            "runId": data.get("runId") or child_id,
            "childSessionId": child_id,
            "parentSessionId": entry.sessionId,
            "spawnEntryId": None,
            "toolCallId": None,
            "agentType": data.get("agentType") or "coder",
            "description": data.get("description") or "sub-agent task",
            "status": "running",
            "background": False,
            "contextMode": "fresh",
            "isolation": "shared",
            "worktreePath": None,
            "groupId": None,
            "model": None,
            "createdAt": entry.timestamp,
            "startedAt": None,
            "endedAt": None,
            "promptEntryId": None,
            "resultEntryId": None,
            "resultPath": None,
            "error": None,
            "resultSummary": None,
            "injectSummary": False,
            "injected": False,
            "pendingSteerCount": 0,
            "pendingApproval": None,
            "metrics": {
                "toolUses": 0,
                "usage": {},
                "turnCount": 0,
                "compactionCount": 0,
                "activeTools": [],
                "currentTool": None,
                "currentToolStartedAt": None,
                "lastEventAt": None,
            },
        })
        for key in (
            "taskId", "runId", "agentType", "description", "status",
            "spawnEntryId", "background", "contextMode", "isolation", "worktreePath",
            "groupId", "model", "resultPath", "error", "resultSummary",
            "pendingApproval", "metrics",
        ):
            if key in data:
                rec[key] = data[key]
        if data.get("event") == "task_started":
            rec["startedAt"] = rec.get("startedAt") or entry.timestamp
            rec["createdAt"] = rec.get("createdAt") or entry.timestamp
        elif data.get("event") == "task_result":
            rec["endedAt"] = entry.timestamp
            rec["pendingApproval"] = None
        elif data.get("event") == "task_status":
            if data.get("action") in {"approval_approved", "approval_denied"}:
                rec["pendingApproval"] = None
            if data.get("status") == "running":
                rec["startedAt"] = rec.get("startedAt") or entry.timestamp
            if data.get("status") in {"completed", "failed", "blocked", "cancelled", "lost", "timed_out"}:
                rec["endedAt"] = rec.get("endedAt") or entry.timestamp
    return out


def task_projection_for_child(child_session_id: str) -> dict[str, Any] | None:
    """Return the parent replay projection for a child session, if available."""
    from .manager import SessionManager

    child = SessionManager.open(child_session_id)
    spawned_by = child.spawned_by() or {}
    parent_session_id = spawned_by.get("sessionId")
    if not parent_session_id or not SessionManager.exists(parent_session_id):
        return None
    parent = SessionManager.open(parent_session_id)
    projection = replay_task_envelopes(parent.entries()).get(child_session_id)
    if projection is None:
        return None
    projection["parentSessionId"] = projection.get("parentSessionId") or parent_session_id
    projection["taskId"] = projection.get("taskId") or spawned_by.get("taskId") or child_session_id
    projection["runId"] = projection.get("runId") or child_session_id
    projection["childSessionId"] = projection.get("childSessionId") or child_session_id
    projection["spawnEntryId"] = projection.get("spawnEntryId") or spawned_by.get("entryId")
    projection["agentType"] = projection.get("agentType") or spawned_by.get("agentType") or "coder"
    projection["description"] = (
        projection.get("description") or spawned_by.get("description") or "sub-agent task")
    return projection
