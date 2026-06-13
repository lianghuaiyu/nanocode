"""entrypoints/interactive/session_select.py — /resume 交互会话浏览器接线（Pi /resume selector UI）。

左列 = 按 parentSession 嵌套的会话树(fork/clone 缩进在父下),右列 = 选中 session 详情(吸收
/session 详情页)。enter resume · r rename · tab 切 scope(current folder/all) · q/esc。
rename 经 EDIT 退出→ask_text→写 session_info(当前 session 用 live writer,其它 session 临时加锁)。
仅 TTY;非 TTY 文本回退在 builtin handler。
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from prompt_toolkit.formatted_text import ANSI

from . import sessionmodel as SM
from .selector import KeyResult, Outcome, SelectorModel, run_selector

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_ACCENT = "\x1b[36m"
_GREEN = "\x1b[32m"


class _SessionModel(SelectorModel):
    def __init__(self, infos: list[SM.SessionInfo], current_sid: str | None, cwd: str,
                 scope: str, now: float) -> None:
        self._all = infos
        self.current_sid = current_sid
        self.cwd = cwd
        self.scope = scope
        self.now = now
        self._flats: list[SM.FlatSession] = []
        self._recompute()

    def _recompute(self) -> None:
        scoped = SM.filter_by_scope(self._all, self.scope, self.cwd)
        self._flats = SM.flatten_session_tree(SM.build_session_tree(scoped))

    def title(self) -> str:
        return f"{_ACCENT}Sessions{_RESET}    {_DIM}scope: {self.scope} · sort: recent{_RESET}"

    def items(self) -> list:
        return self._flats

    def list_text(self, item: SM.FlatSession, selected: bool, width: int) -> Any:
        i = item.info
        cursor = f"{_ACCENT}› {_RESET}" if selected else "  "
        prefix = SM.tree_prefix(item)
        title = i.name or i.first_message or "(empty)"
        age = SM.format_session_date(i.modified, self.now)
        cur = f"  {_ACCENT}← current{_RESET}" if i.sid == self.current_sid else ""
        right = f"{_DIM}{i.message_count} · {age} · {i.origin}{_RESET}"
        color = _GREEN if i.name else ""
        return ANSI(f"{cursor}{_DIM}{prefix}{_RESET}{color}{title[:40]}{_RESET}  {right}{cur}")

    def preview_text(self, item: SM.FlatSession, width: int) -> list[str]:
        return SM.session_detail_lines(item.info)

    def hint(self) -> str:
        return f"{_DIM}↑↓ move · enter resume · r rename · tab scope · q/esc{_RESET}"

    def extra_keys(self) -> tuple[str, ...]:
        return ("r", "tab")

    def on_key(self, key: str, item: SM.FlatSession, index: int) -> KeyResult | None:
        if key == "tab":
            self.scope = "all" if self.scope == "current" else "current"
            self._recompute()
            return KeyResult("refresh")
        if key == "r" and item is not None:
            return KeyResult("edit", edit_action="rename")
        return None


def _write_name(sid: str, current_sid: str | None, current_mgr, text: str) -> str | None:
    """给 session 设名:当前 session 用 live writer;其它 session 临时加锁写后关。返回错误串或 None。"""
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


async def run_sessions(*, current_sid: str | None, cwd: str, current_mgr,
                       ask_text: Callable[[str], Awaitable[str | None]]) -> dict | None:
    """跑 /sessions 交互循环。返回 {"action":"resume","sid":id} 或 None(取消)。
    rename 就地写后重跑(保持光标+scope)。"""
    scope = "current"
    index = 0
    while True:
        infos = SM.scan_sessions()
        model = _SessionModel(infos, current_sid, cwd, scope, time.time())
        outcome: Outcome = await run_selector(model, initial_index=index)
        scope = model.scope
        index = outcome.index
        if outcome.kind == "cancel":
            return None
        if outcome.kind == "done":
            return {"action": "resume", "sid": outcome.item.info.sid}
        if outcome.kind == "edit" and outcome.edit_action == "rename":
            i = outcome.item.info
            text = await ask_text(f"rename session {i.sid[-8:]} (blank=clear) [{i.name or ''}]: ")
            if text is not None:
                err = _write_name(i.sid, current_sid, current_mgr, text.strip())
                if err:
                    print(f"(rename failed: {err})")
            continue
