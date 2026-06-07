"""Session v2：目录事件流存储（state + main/agent messages + task 目录）。
与旧 flat JSON session 并存；engine 接线在后续阶段。"""
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


def write_main_messages(session_id: str, messages: list) -> None:
    _write_json(session_root(session_id) / "main" / "messages.json", messages)


def read_main_messages(session_id: str) -> list:
    return _read_json(session_root(session_id) / "main" / "messages.json", [])


def write_agent_messages(session_id: str, agent_id: str, messages: list) -> None:
    _write_json(session_root(session_id) / "agents" / agent_id / "messages.json", messages)


def read_agent_messages(session_id: str, agent_id: str) -> list:
    return _read_json(session_root(session_id) / "agents" / agent_id / "messages.json", [])


def task_dir(session_id: str, task_id: str) -> Path:
    d = session_root(session_id) / "tasks" / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d
