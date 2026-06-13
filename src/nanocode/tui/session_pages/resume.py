"""Resume-session page — Pi single-column layout (header on top, no preview).

Owns selector state; delegates listing to ``nanocode.session.listing``. Mirrors Pi
``session-selector.ts`` UX: bordered full-width list, search-on-top, scope/sort/name/path
toggles, rename, and Ctrl+D delete (with confirm + current-session protection).
"""

from __future__ import annotations

import time
from typing import Any

from prompt_toolkit.formatted_text import ANSI

from ...session import listing as SL
from ...session import search as SS
from ..selector import KeyResult, Outcome, SelectorModel

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_REV = "\x1b[7m"
_ACCENT = "\x1b[36m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"

_MAX_VISIBLE = 10  # Pi session-selector.ts:296


def _truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    if width <= 1:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _short_path(path: str, width: int = 28) -> str:
    if not path:
        return ""
    import os
    home = os.path.expanduser("~")
    shown = path
    if home and path == home:
        shown = "~"
    elif home and path.startswith(home + "/"):
        shown = "~" + path[len(home):]
    if len(shown) <= width:
        return shown
    parts = shown.split("/")
    if len(parts) >= 2:
        return "…" + "/".join(parts[-2:])[-(width - 1):]
    return "…" + shown[-(width - 1):]


def _cycle_sort(mode: SS.SortMode) -> SS.SortMode:
    return {"threaded": "recent", "recent": "relevance", "relevance": "threaded"}[mode]  # type: ignore[return-value]


def _justify(left_plain: str, left_ansi: str, right_plain: str, right_ansi: str, width: int) -> str:
    spacing = max(2, width - len(left_plain) - len(right_plain))
    return left_ansi + " " * spacing + right_ansi


class ResumeSessionModel(SelectorModel):
    def __init__(self, infos: list[SL.SessionInfo], current_sid: str | None, cwd: str,
                 scope: str, now: float, *, query: str = "", sort_mode: SS.SortMode = "threaded",
                 name_filter: SS.NameFilter = "all", show_path: bool = False,
                 status: str = "") -> None:
        self._all = list(infos)
        self.current_sid = current_sid
        self.cwd = cwd
        self.scope = scope
        self.now = now
        self._query = query
        self.sort_mode: SS.SortMode = sort_mode
        self.name_filter: SS.NameFilter = name_filter
        self.show_path = show_path
        self.status = status
        self._confirm = False
        self._scoped_count = 0
        self._flats: list[SL.FlatSession] = []
        self._recompute()

    def _recompute(self) -> None:
        scoped = SL.filter_by_scope(self._all, self.scope, self.cwd)
        self._scoped_count = len(scoped)
        filtered = SS.filter_and_sort_sessions(scoped, self._query, self.sort_mode, self.name_filter)
        if self.sort_mode == "threaded" and not self._query.strip():
            self._flats = SL.flatten_session_tree(SL.build_session_tree(filtered))
        else:
            self._flats = [SL.FlatSession(info=s, depth=0, is_last=True) for s in filtered]

    # ── chrome ───────────────────────────────────────────────
    def max_visible(self, height: int) -> int:
        return _MAX_VISIBLE

    def confirming(self) -> bool:
        return self._confirm

    def header_lines(self, width: int) -> list[Any]:
        title = "Resume Session (Current Folder)" if self.scope == "current" else "Resume Session (All)"
        scope_plain = ("◉ Current Folder | ○ All" if self.scope == "current"
                       else "○ Current Folder | ◉ All")
        name = "All" if self.name_filter == "all" else "Named"
        sort = {"threaded": "Threaded", "recent": "Recent", "relevance": "Fuzzy"}[self.sort_mode]
        right_plain = f"{scope_plain}  Name: {name}  Sort: {sort}"
        right_ansi = f"{_DIM}{scope_plain}  Name: {_RESET}{_ACCENT}{name}{_RESET}{_DIM}  Sort: {_RESET}{_ACCENT}{sort}{_RESET}"
        line1 = _justify(title, f"  {_BOLD}{title}{_RESET}", right_plain, right_ansi, width)
        if self._confirm:
            line2 = f"  {_RED}Delete this session? <enter> confirm · <esc> cancel{_RESET}"
            line3 = ""
        elif self.status:
            line2 = f"  {_ACCENT}{self.status}{_RESET}"
            line3 = ""
        else:
            line2 = f"  {_DIM}Tab scope · re:<pattern> regex · \"phrase\" exact{_RESET}"
            path = "on" if self.show_path else "off"
            line3 = f"  {_DIM}Ctrl+S sort · Ctrl+N named · Ctrl+D delete · Ctrl+P path ({path}) · Ctrl+R rename{_RESET}"
        return [ANSI(line1), ANSI(line2), ANSI(line3)]

    def search_line(self, width: int) -> Any:
        parsed = SS.parse_search_query(self._query)
        count = "regex error" if parsed.error else f"{len(self._flats)}/{self._scoped_count}"
        return ANSI(f"  {_DIM}Search:{_RESET} {self._query}    {_DIM}{count}{_RESET}")

    def items(self) -> list:
        return self._flats

    def list_text(self, item: SL.FlatSession, selected: bool, width: int) -> Any:
        info = item.info
        prefix = SL.tree_prefix(item)
        title = info.name or info.first_message or "(empty)"
        age = SL.format_session_date(info.modified, self.now)
        right_parts = [str(info.message_count), age]
        if self.scope == "all" and info.cwd:
            right_parts.insert(0, _short_path(info.cwd, 22))
        if self.show_path:
            right_parts.insert(0, _short_path(info.path, 24))
        right = " ".join(right_parts)
        cursor = "› " if selected else "  "
        avail = max(10, width - len(prefix) - len(right) - len(cursor) - 2)
        t = _truncate(title, avail)
        pad = max(1, width - len(cursor) - len(prefix) - len(t) - len(right) - 1)
        if selected:
            line = f"{cursor}{prefix}{t}{' ' * pad}{right} "
            return ANSI(f"{_REV}{line[:width]}{_RESET}")
        color = _YELLOW if info.name else (_ACCENT if info.sid == self.current_sid else "")
        return ANSI(f"{cursor}{_DIM}{prefix}{_RESET}{color}{t}{_RESET}{' ' * pad}{_DIM}{right}{_RESET}")

    # ── live search ──────────────────────────────────────────
    def supports_query(self) -> bool:
        return not self._confirm

    def query(self) -> str:
        return self._query

    def set_query(self, query: str) -> None:
        self._query = query
        self.status = ""
        self._recompute()

    # ── keys ─────────────────────────────────────────────────
    def extra_keys(self) -> tuple[str, ...]:
        return ("tab", "c-s", "c-n", "c-p", "c-r", "c-d")

    def on_key(self, key: str, item: SL.FlatSession, index: int) -> KeyResult | None:
        if self._confirm:
            if key == "confirm":
                return self._do_delete(item)
            if key == "abort":
                self._confirm = False
                self.status = "delete cancelled"
                return KeyResult("refresh")
            return KeyResult("continue")
        if key == "tab":
            self.scope = "all" if self.scope == "current" else "current"
            self.status = ""
            self._recompute()
            return KeyResult("refresh")
        if key == "c-s":
            self.sort_mode = _cycle_sort(self.sort_mode)
            self.status = f"sort: {self.sort_mode}"
            self._recompute()
            return KeyResult("refresh")
        if key == "c-n":
            self.name_filter = "named" if self.name_filter == "all" else "all"
            self.status = "named sessions only" if self.name_filter == "named" else "showing all sessions"
            self._recompute()
            return KeyResult("refresh")
        if key == "c-p":
            self.show_path = not self.show_path
            self.status = "path shown" if self.show_path else "path hidden"
            return KeyResult("refresh")
        if key == "c-d" and item is not None:
            if item.info.sid == self.current_sid:
                self.status = "cannot delete the current session"
                return KeyResult("refresh")
            self._confirm = True
            return KeyResult("refresh")
        if key == "c-r" and item is not None:
            return KeyResult("edit", edit_action="rename")
        return None

    def _do_delete(self, item: SL.FlatSession) -> KeyResult:
        self._confirm = False
        if item is None:
            return KeyResult("refresh")
        sid = item.info.sid
        if sid == self.current_sid:
            self.status = "cannot delete the current session"
            return KeyResult("refresh")
        self.status = SL.delete_session(sid)
        self._all = [i for i in self._all if i.sid != sid]
        self._recompute()
        return KeyResult("refresh")


def _write_name(sid: str, current_sid: str | None, current_mgr, text: str) -> str | None:
    if sid == current_sid and current_mgr is not None:
        current_mgr.append_session_info(text)
        return None
    from ...session.manager import SessionManager
    from ...session.tree import SessionBusyError

    try:
        mgr = SessionManager.open(sid, lock=True)
    except SessionBusyError:
        return f"session {sid[-8:]} is busy (held by another writer)"
    try:
        mgr.append_session_info(text)
    finally:
        mgr.close()
    return None


async def run_sessions(*, current_sid: str | None, cwd: str, current_mgr, host) -> dict | None:
    """Run the resume page; returns ``{"action": "resume", "sid": id}`` or None."""

    scope = "current"
    index = 0
    query = ""
    sort_mode: SS.SortMode = "threaded"
    name_filter: SS.NameFilter = "all"
    show_path = False
    status = ""
    while True:
        infos = SL.scan_sessions()
        model = ResumeSessionModel(
            infos, current_sid, cwd, scope, time.time(), query=query, sort_mode=sort_mode,
            name_filter=name_filter, show_path=show_path, status=status,
        )
        status = ""
        outcome: Outcome = await host.run_selector(model, initial_index=index)
        scope, query = model.scope, model.query()
        sort_mode, name_filter, show_path = model.sort_mode, model.name_filter, model.show_path
        index = outcome.index
        if outcome.kind == "cancel":
            return None
        if outcome.kind == "done":
            return {"action": "resume", "sid": outcome.item.info.sid}
        if outcome.kind == "edit" and outcome.edit_action == "rename":
            info = outcome.item.info
            text = await host.ask_text(f"rename session {info.sid[-8:]} (blank=clear) [{info.name or ''}]: ")
            if text is not None:
                err = _write_name(info.sid, current_sid, current_mgr, text.strip())
                status = (f"rename failed: {err}" if err
                          else ("session renamed" if text.strip() else "session name cleared"))
            continue
