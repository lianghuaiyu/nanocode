"""entrypoints/interactive/sessionmodel.py — /sessions 的纯逻辑(移植 pi session-selector.ts)。

`scan_sessions()` 做 I/O(读每个 session.jsonl 聚合 SessionInfo);其余(build_session_tree /
flatten / 相对时间 / 详情)是纯函数,吃 SessionInfo 列表,可单测。lineage 不靠 origin 字符串而靠
**按 parentSession 嵌套**(fork/clone 子会话缩进在父下),与 pi 一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...session import tree as T


@dataclass
class SessionInfo:
    sid: str
    name: str | None
    first_message: str
    message_count: int
    modified: float            # epoch 秒(session 文件 mtime)
    cwd: str
    parent_sid: str | None
    origin: str                # 'root' | 'fork' | 'clone'
    leaf: str | None
    latest_role: str | None
    latest_text: str


def _msg_text(e: T.Entry) -> str:
    c = (e.data.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def scan_sessions() -> list[SessionInfo]:
    """读所有 canonical session 聚合 SessionInfo(I/O)。损坏/打不开的跳过。"""
    from ...session.manager import SessionManager, _scan_headers, session_file
    out: list[SessionInfo] = []
    for sid, ps in _scan_headers():
        try:
            mgr = SessionManager.open(sid)
        except Exception:
            continue
        entries = mgr.entries()
        msgs = [e for e in entries if e.type == T.MESSAGE]
        first = ""
        for e in msgs:
            if (e.data.get("message") or {}).get("role") == "user":
                first = _msg_text(e).replace("\n", " ").strip()
                break
        last = msgs[-1] if msgs else None
        try:
            mtime = session_file(sid).stat().st_mtime
        except Exception:
            mtime = 0.0
        origin = "root" if not ps else ("fork" if ps.get("forkedBeforeEntryId") else "clone")
        out.append(SessionInfo(
            sid=sid, name=mgr.name(), first_message=first, message_count=len(msgs),
            modified=mtime, cwd=mgr._cwd(), parent_sid=(ps or {}).get("sessionId"),
            origin=origin, leaf=mgr.get_leaf(),
            latest_role=(last.data.get("message") or {}).get("role") if last else None,
            latest_text=(_msg_text(last).replace("\n", " ").strip() if last else ""),
        ))
    return out


def format_session_date(modified: float, now: float) -> str:
    """相对时间(pi formatSessionDate):now/5m/2h/3d/2w/3mo/1y。"""
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


# ─── 按 parentSession 嵌套的会话树 ─────────────────────────────────────────────

@dataclass
class SessionNode:
    info: SessionInfo
    children: list["SessionNode"] = field(default_factory=list)


@dataclass
class FlatSession:
    info: SessionInfo
    depth: int
    is_last: bool


def build_session_tree(infos: list[SessionInfo]) -> list[SessionNode]:
    """按 parent_sid 嵌套;root 与各层按 modified 降序。父不在集合内者作 root。"""
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
    out: list[FlatSession] = []

    def walk(node: SessionNode, depth: int, is_last: bool) -> None:
        out.append(FlatSession(node.info, depth, is_last))
        for idx, c in enumerate(node.children):
            walk(c, depth + 1, idx == len(node.children) - 1)
    for idx, r in enumerate(roots):
        walk(r, 0, idx == len(roots) - 1)
    return out


def tree_prefix(flat: FlatSession) -> str:
    if flat.depth == 0:
        return ""
    return "  " * (flat.depth - 1) + ("└ " if flat.is_last else "├ ")


def session_detail_lines(info: SessionInfo) -> list[str]:
    """/sessions 右栏 = /session 详情(吸收详情页)。"""
    lines = [
        f"{info.sid}",
        "",
        f"origin   {info.origin}" + (f"  (parent {info.parent_sid[-8:]})" if info.parent_sid else ""),
        f"cwd      {info.cwd}",
        f"entries  {info.message_count}    leaf …{str(info.leaf)[-8:]}",
    ]
    if info.name:
        lines.append(f"name     {info.name}")
    if info.latest_role:
        lines.append(f"latest   {info.latest_role}: {info.latest_text[:60]}")
    return lines


def filter_by_scope(infos: list[SessionInfo], scope: str, cwd: str) -> list[SessionInfo]:
    """scope='current' → 仅当前 cwd 的 session;'all' → 全部。"""
    if scope == "current":
        return [i for i in infos if i.cwd == cwd]
    return infos


def render_sessions_text(infos: list[SessionInfo], current_sid: str | None, now: float) -> list[str]:
    """非 TTY 文本回退 + 单测。"""
    flats = flatten_session_tree(build_session_tree(infos))
    if not flats:
        return ["  (no sessions)"]
    out: list[str] = []
    for f in flats:
        i = f.info
        mark = "  ← current" if i.sid == current_sid else ""
        title = i.name or i.first_message or "(empty)"
        age = format_session_date(i.modified, now)
        out.append(f"  {tree_prefix(f)}{i.sid[-8:]}  {title[:40]}  {i.message_count} msgs · {age} · {i.origin}{mark}")
    return out
