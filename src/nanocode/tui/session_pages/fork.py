"""Fork page — flat list of user messages (Pi ``UserMessageSelectorComponent``).

``/fork`` picks a past user message; a new session is forked to **before** it and the
message is pre-filled into the editor. This is a separate flat selector, NOT the tree page.
"""

from __future__ import annotations

from typing import Any


from ...session import tree as T
from ..selector import Outcome, SelectorModel, cell_width, pad_cells, truncate_cells
from ..theme import BOLD as _BOLD, DIM as _DIM, RESET as _RESET, fg as _fg

_ACCENT = _fg("accent")


def _truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    return truncate_cells(text, width)


def is_user_message(e: T.Entry) -> bool:
    return e.type == T.MESSAGE and (e.data.get("message") or {}).get("role") == "user"


def user_text(e: T.Entry) -> str:
    c = (e.data.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


class ForkModel(SelectorModel):
    def __init__(self, entries: list[T.Entry], *, leaf_id: str | None = None, query: str = "") -> None:
        self.entries = entries
        self.leaf_id = leaf_id
        self._users = list(reversed([e for e in entries if is_user_message(e)]))  # newest first
        self._query = query
        self._rows: list[T.Entry] = []
        self._recompute()

    def _recompute(self) -> None:
        q = self._query.lower().split()
        self._rows = [e for e in self._users
                      if all(t in user_text(e).lower() for t in q)] if q else list(self._users)

    def max_visible(self, height: int) -> int:
        return max(5, height // 2)

    def header_lines(self, width: int) -> list[Any]:
        sid = self.entries[0].sessionId[-8:] if self.entries else "?"
        return [
            f"  {_BOLD}Fork from user message{_RESET}{_DIM} · {sid}{_RESET}",
            f"  {_DIM}↑↓ move · enter fork (new session before this message, prompt pre-filled) · "
                 f"type to search · esc cancel{_RESET}",
        ]

    def search_line(self, width: int) -> Any:
        return f"  {_DIM}Search:{_RESET} {self._query}    {_DIM}{len(self._rows)}/{len(self._users)}{_RESET}"

    def items(self) -> list:
        return self._rows

    def initial_index(self) -> int:
        if not self._rows or self.leaf_id is None:
            return 0
        try:
            branch = T.get_branch(T.index_by_id(self.entries), self.leaf_id)
        except Exception:
            return 0
        branch_users = [e.id for e in branch if is_user_message(e)]
        if not branch_users:
            return 0
        target = branch_users[-1]
        for i, row in enumerate(self._rows):
            if row.id == target:
                return i
        return 0

    def list_text(self, item: T.Entry, selected: bool, width: int) -> Any:
        cursor = "› " if selected else "  "
        text = _truncate(user_text(item) or "(empty)", max(1, width - cell_width(cursor)))
        if selected:                               # Pi:accent '› ' 游标 + bold(无反显)
            return f"{_ACCENT}{cursor}{_RESET}{_BOLD}{text}{_RESET}"
        return f"{cursor}{text}"

    def supports_query(self) -> bool:
        return True

    def query(self) -> str:
        return self._query

    def set_query(self, query: str) -> None:
        self._query = query
        self._recompute()


async def run_fork(manager, *, host) -> dict | None:
    """Run the fork page; returns ``{"action": "fork", "entry_id": id}`` or None."""

    index: int | None = None
    query = ""
    while True:
        entries = manager.entries()
        model = ForkModel(entries, leaf_id=manager.get_leaf(), query=query)
        outcome: Outcome = await host.run_selector(model, initial_index=index)
        query = model.query()
        index = outcome.index
        if outcome.kind == "cancel":
            return None
        if outcome.kind == "done":
            return {"action": "fork", "entry_id": outcome.item.id}
        # fork page has no edit actions
        return None
