"""Session listing and tree projection for resume pages.

This module mirrors Pi's split: durable session reading lives in the session
layer, while TUI pages consume the structured ``SessionInfo`` records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import tree as T


@dataclass
class SessionInfo:
    sid: str
    path: str
    name: str | None
    first_message: str
    all_messages_text: str
    message_count: int
    created: float
    modified: float
    cwd: str
    parent_sid: str | None
    origin: str
    leaf: str | None
    latest_role: str | None
    latest_text: str


def message_text(e: T.Entry) -> str:
    c = (e.data.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _timestamp_epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _session_created(entries: list[T.Entry]) -> float:
    for e in entries:
        if e.type == T.SESSION_START:
            t = _timestamp_epoch(e.timestamp)
            if t is not None:
                return t
    return 0.0


def _message_activity_time(e: T.Entry) -> float | None:
    msg = e.data.get("message") or {}
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    t = msg.get("timestamp")
    if isinstance(t, (int, float)):
        return float(t / 1000 if t > 10_000_000_000 else t)
    return _timestamp_epoch(e.timestamp)


def scan_sessions() -> list[SessionInfo]:
    """Read all canonical sessions and aggregate resume-page metadata."""

    from .manager import SessionManager, _scan_headers, session_file

    out: list[SessionInfo] = []
    for sid, ps in _scan_headers():
        path: Path = session_file(sid)
        try:
            mgr = SessionManager.open(sid)
        except Exception:
            continue
        entries = mgr.entries()
        msgs = [e for e in entries if e.type == T.MESSAGE]
        first = ""
        all_messages: list[str] = []
        last_activity = 0.0
        for e in msgs:
            msg = e.data.get("message") or {}
            role = msg.get("role")
            if role in ("user", "assistant"):
                text = message_text(e).replace("\n", " ").strip()
                if text:
                    all_messages.append(text)
                    if not first and role == "user":
                        first = text
                t = _message_activity_time(e)
                if t is not None:
                    last_activity = max(last_activity, t)
        last = msgs[-1] if msgs else None
        created = _session_created(entries)
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        modified = last_activity or created or mtime
        origin = "root" if not ps else ("fork" if ps.get("forkedBeforeEntryId") else "clone")
        name = mgr.name()
        if name is None and not msgs:
            continue
        out.append(SessionInfo(
            sid=sid,
            path=str(path),
            name=name,
            first_message=first,
            all_messages_text=" ".join(all_messages),
            message_count=len(msgs),
            created=created or mtime,
            modified=modified,
            cwd=mgr._cwd(),
            parent_sid=(ps or {}).get("sessionId"),
            origin=origin,
            leaf=mgr.get_leaf(),
            latest_role=(last.data.get("message") or {}).get("role") if last else None,
            latest_text=(message_text(last).replace("\n", " ").strip() if last else ""),
        ))
    return out


def format_session_date(modified: float, now: float) -> str:
    """Relative age in Pi style: now/5m/2h/3d/2w/3mo/1y."""

    diff = max(0.0, now - modified)
    mins = int(diff // 60)
    hours = int(diff // 3600)
    days = int(diff // 86400)
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    if hours < 24:
        return f"{hours}h"
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


@dataclass
class SessionNode:
    info: SessionInfo
    children: list["SessionNode"] = field(default_factory=list)


@dataclass
class FlatSession:
    info: SessionInfo
    depth: int
    is_last: bool
    prefix: str = ""   # 预算好的 Pi 3-char gutter 前缀（│  / └─ / ├─），见 flatten_session_tree


def build_session_tree(infos: list[SessionInfo]) -> list[SessionNode]:
    """Nest sessions by parent sid; roots and children sort by modified desc."""

    by_sid = {i.sid: SessionNode(info=i) for i in infos}
    roots: list[SessionNode] = []
    for i in infos:
        node = by_sid[i.sid]
        parent = by_sid.get(i.parent_sid) if i.parent_sid else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)

    def sort_nodes(nodes: list[SessionNode]) -> None:
        nodes.sort(key=lambda n: n.info.modified, reverse=True)
        for n in nodes:
            sort_nodes(n.children)

    sort_nodes(roots)
    return roots


def flatten_session_tree(roots: list[SessionNode]) -> list[FlatSession]:
    """Flatten + precompute Pi-style 3-char gutter prefixes（`│  ` 续接 / `   ` 空 + `└─ `/`├─ `）。

    对位 Pi `session-selector.ts:508-516` buildTreePrefix：祖先列续接画 `│  `、否则空格，末级画
    `└─ `(last)/`├─ `(非 last)；root 无前缀。"""
    out: list[FlatSession] = []

    def walk(node: SessionNode, gutter: str, is_last: bool, is_root: bool) -> None:
        prefix = "" if is_root else gutter + ("└─ " if is_last else "├─ ")
        # depth = 缩进层级（兼容旧字段）：root=0，其余按祖先 gutter 段数 + 1
        depth_level = 0 if is_root else (len(gutter) // 3) + 1
        out.append(FlatSession(node.info, depth_level, is_last, prefix))
        child_gutter = "" if is_root else gutter + ("   " if is_last else "│  ")
        kids = node.children
        for idx, child in enumerate(kids):
            walk(child, child_gutter, idx == len(kids) - 1, False)

    for idx, root in enumerate(roots):
        walk(root, "", idx == len(roots) - 1, True)
    return out


def tree_prefix(flat: FlatSession) -> str:
    """Pi 3-char gutter 前缀（flatten 时已算好；flat-mode 构造的 FlatSession 默认空）。"""
    return flat.prefix


def session_detail_lines(info: SessionInfo) -> list[str]:
    """Preview panel for a selected session."""

    lines = [
        f"{info.sid}",
        "",
        f"title    {info.name or info.first_message or '(empty)'}",
        f"origin   {info.origin}" + (f"  (parent {info.parent_sid[-8:]})" if info.parent_sid else ""),
        f"cwd      {info.cwd}",
        f"path     {info.path}",
        f"entries  {info.message_count} messages    leaf …{str(info.leaf)[-8:]}",
    ]
    if info.latest_role:
        lines.append(f"latest   {info.latest_role}: {info.latest_text[:60]}")
    if info.first_message:
        lines.append("")
        lines.append(f"first    {info.first_message[:90]}")
    return lines


def filter_by_scope(infos: list[SessionInfo], scope: str, cwd: str) -> list[SessionInfo]:
    if scope == "current":
        return [i for i in infos if i.cwd == cwd]
    return infos


def delete_session(sid: str) -> str:
    """Delete a session file. Try the `trash` CLI first (recoverable), else unlink.

    对位 Pi `session-selector.ts:631-666`。返回人面状态串（成功/失败）。调用方负责**禁止删除当前
    active session**（见 resume 页）。"""
    import shutil
    import subprocess
    from .manager import session_file

    path = session_file(sid)
    if not path.exists():
        return f"session {sid[-8:]} not found"
    trash = shutil.which("trash")
    if trash:
        try:
            r = subprocess.run([trash, str(path)], capture_output=True, timeout=10)
            if r.returncode == 0:
                return "session moved to trash"
        except Exception:
            pass
    try:
        path.unlink()
        return "session deleted"
    except Exception as e:
        return f"delete failed: {e}"


def render_sessions_text(infos: list[SessionInfo], current_sid: str | None, now: float) -> list[str]:
    flats = flatten_session_tree(build_session_tree(infos))
    if not flats:
        return ["  (no sessions)"]
    out: list[str] = []
    for flat in flats:
        info = flat.info
        mark = "  ← current" if info.sid == current_sid else ""
        title = info.name or info.first_message or "(empty)"
        age = format_session_date(info.modified, now)
        out.append(
            f"  {tree_prefix(flat)}{info.sid[-8:]}  {title[:40]}  "
            f"{info.message_count} msgs · {age} · {info.origin}{mark}"
        )
    return out
