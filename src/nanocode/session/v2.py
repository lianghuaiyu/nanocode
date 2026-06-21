"""Session v2 derived state and host task directories.

Conversation history is authoritative in ``session.jsonl``. Sub-agent durable
state lives under each child session's ``subagent-run/`` sidecar.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import sessions_dir


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
