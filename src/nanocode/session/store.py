"""Session management — JSON file persistence for conversation history."""

from __future__ import annotations

import json
from typing import Any

from ..paths import sessions_dir
from . import v2


def save_session(session_id: str, data: dict[str, Any]) -> None:
    (sessions_dir() / f"{session_id}.json").write_text(json.dumps(data, indent=2, default=str))


def load_session(session_id: str) -> dict[str, Any] | None:
    path = sessions_dir() / f"{session_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    if v2.is_v2_session(session_id):
        msgs = v2.read_main_messages(session_id)
        return {"v2": True, "session_id": session_id, "state": v2.read_state(session_id),
                "anthropicMessages": msgs, "openaiMessages": msgs}
    return None


def list_sessions() -> list[dict[str, Any]]:
    results = []
    for f in sessions_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if "metadata" in data:
                results.append(data["metadata"])
        except Exception:
            pass
    return results


def get_latest_session_id() -> str | None:
    candidates = [(m.get("startTime", ""), m.get("id")) for m in list_sessions()]
    d = sessions_dir()
    if d.exists():
        for entry in d.iterdir():
            if entry.is_dir():
                st = v2.read_state(entry.name)
                if st:
                    candidates.append((st.get("startTime", ""), st.get("id") or entry.name))
    candidates = [(t, i) for t, i in candidates if i]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]
