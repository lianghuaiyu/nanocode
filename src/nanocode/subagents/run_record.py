"""Child-session-owned durable run record helpers.

The sidecar is an operational projection under a child session directory. It
never creates session lineage and never replaces ``session.jsonl`` as replay or
compaction authority.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..session import tree
from ..session.manager import session_root

RUN_DIR_NAME = "subagent-run"
STATUS_FILE = "status.json"
EVENTS_FILE = "events.jsonl"
PROMPT_FILE = "prompt.md"
RESULT_FILE = "result.md"
PENDING_STEER_FILE = "pending_steer.jsonl"


def run_dir(child_session_id: str) -> Path:
    return session_root(child_session_id) / RUN_DIR_NAME


def status_path(child_session_id: str) -> Path:
    return run_dir(child_session_id) / STATUS_FILE


def events_path(child_session_id: str) -> Path:
    return run_dir(child_session_id) / EVENTS_FILE


def prompt_path(child_session_id: str) -> Path:
    return run_dir(child_session_id) / PROMPT_FILE


def result_path(child_session_id: str) -> Path:
    return run_dir(child_session_id) / RESULT_FILE


def pending_steer_path(child_session_id: str) -> Path:
    return run_dir(child_session_id) / PENDING_STEER_FILE


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


def _now() -> str:
    return tree.now_iso()


def read_status(child_session_id: str) -> dict[str, Any]:
    return json.loads(status_path(child_session_id).read_text(encoding="utf-8"))


def write_status(child_session_id: str, status: dict[str, Any]) -> None:
    _write_json_atomic(status_path(child_session_id), status)


def update_status(child_session_id: str, **fields: Any) -> dict[str, Any]:
    status = read_status(child_session_id)
    status.update(fields)
    write_status(child_session_id, status)
    return status


def append_event(child_session_id: str, event_type: str, **data: Any) -> dict[str, Any]:
    event = {"type": event_type, "timestamp": _now(), **data}
    _append_jsonl(events_path(child_session_id), event)
    try:
        status = read_status(child_session_id)
        metrics = dict(status.get("metrics") or {})
        metrics["lastEventAt"] = event["timestamp"]
        status["metrics"] = metrics
        write_status(child_session_id, status)
    except FileNotFoundError:
        pass
    return event


def read_events(child_session_id: str) -> list[dict[str, Any]]:
    path = events_path(child_session_id)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def write_prompt(child_session_id: str, content: str) -> str:
    path = prompt_path(child_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def write_result(child_session_id: str, content: str) -> str:
    path = result_path(child_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
    return str(path)


def read_result(child_session_id: str) -> str:
    path = result_path(child_session_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def ensure_pending_file(child_session_id: str) -> None:
    path = pending_steer_path(child_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def create_run_record(
    *,
    child_session_id: str,
    parent_session_id: str,
    spawn_entry_id: str | None,
    tool_call_id: str | None,
    agent_type: str,
    background: bool,
    context_mode: str,
    isolation: str,
    worktree_path: str | None,
    model: dict[str, Any],
    prompt: str,
    status: str = "running",
) -> dict[str, Any]:
    now = _now()
    rd = run_dir(child_session_id)
    rd.mkdir(parents=True, exist_ok=True)
    prompt_doc = (
        f"# Subagent Prompt\n\n"
        f"- runId: {child_session_id}\n"
        f"- parentSessionId: {parent_session_id}\n"
        f"- agentType: {agent_type}\n"
        f"- contextMode: {context_mode}\n"
        f"- isolation: {isolation}\n"
        f"- worktreePath: {worktree_path or ''}\n\n"
        f"## Prompt\n\n{prompt or ''}\n"
    )
    write_prompt(child_session_id, prompt_doc)
    ensure_pending_file(child_session_id)
    snapshot = {
        "schemaVersion": 1,
        "runId": child_session_id,
        "childSessionId": child_session_id,
        "parentSessionId": parent_session_id,
        "spawnEntryId": spawn_entry_id,
        "toolCallId": tool_call_id,
        "agentType": agent_type,
        "status": status,
        "background": background,
        "contextMode": context_mode,
        "isolation": isolation,
        "worktreePath": worktree_path,
        "model": model,
        "createdAt": now,
        "startedAt": now if status == "running" else None,
        "endedAt": None,
        "promptEntryId": None,
        "resultEntryId": None,
        "resultPath": None,
        "error": None,
        "pendingSteerCount": 0,
        "metrics": {
            "toolUses": 0,
            "usage": {},
            "turnCount": 0,
            "compactionCount": 0,
            "activeTools": [],
            "lastEventAt": None,
        },
    }
    write_status(child_session_id, snapshot)
    append_event(child_session_id, "created", status=status, background=background)
    return snapshot


def complete_run(
    child_session_id: str,
    *,
    status: str,
    result: str,
    result_entry_id: str | None,
    prompt_entry_id: str | None = None,
    error: str | None = None,
    tokens: dict[str, int] | None = None,
) -> dict[str, Any]:
    path = write_result(child_session_id, result or "")
    snapshot = read_status(child_session_id)
    metrics = dict(snapshot.get("metrics") or {})
    if tokens is not None:
        metrics["usage"] = {
            "input": int(tokens.get("input") or 0),
            "output": int(tokens.get("output") or 0),
        }
    snapshot.update({
        "status": status,
        "endedAt": _now(),
        "resultEntryId": result_entry_id,
        "resultPath": path,
        "error": error,
        "metrics": metrics,
    })
    if prompt_entry_id is not None:
        snapshot["promptEntryId"] = prompt_entry_id
    write_status(child_session_id, snapshot)
    event_type = "completed" if status == "completed" else (
        "cancelled" if status == "cancelled" else "failed")
    append_event(child_session_id, event_type, status=status, resultPath=path, error=error)
    return snapshot
