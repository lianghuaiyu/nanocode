"""Session discovery — canonical session.jsonl 树是唯一来源（docs/16 C-3：legacy flat/v2 发现面已删）。"""

from __future__ import annotations

import json

from ..paths import sessions_dir


def get_latest_session_id() -> str | None:
    """最近的 top-level canonical session（session.jsonl header timestamp 排序；child session——有
    parentSession——不作 latest resume 目标）。"""
    candidates: list[tuple[str, str]] = []
    d = sessions_dir()
    if d.exists():
        for entry in d.iterdir():
            if not entry.is_dir():
                continue
            sj = entry / "session.jsonl"
            if not sj.exists():
                continue
            try:
                h = json.loads(sj.open(encoding="utf-8").readline() or "{}")
            except Exception:
                h = {}
            if h.get("type") == "session_start" and not (h.get("data") or {}).get("parentSession"):
                candidates.append((h.get("timestamp", ""), h.get("sessionId") or entry.name))
    candidates = [(t, i) for t, i in candidates if i]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]
