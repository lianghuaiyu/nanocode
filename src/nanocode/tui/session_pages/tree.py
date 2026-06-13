"""Session tree page — Pi single-column layout with fold/unfold + always-live search.

Mirrors Pi ``tree-selector.ts``: bordered full-width tree, ASCII connectors + fold glyphs
(⊟/⊞), active-path marker, filter chords (Ctrl+D/T/U/L/A + Ctrl+O cycle), Ctrl+Left/Right
fold/unfold, Shift+L label, type-to-search (whitespace-AND substring). No preview, no id column.
``/fork`` is a separate flat page (see ``fork.py``).
"""

from __future__ import annotations

from typing import Any

from prompt_toolkit.formatted_text import ANSI

from ...session import tree as T
from ...session import tree_view as TV
from ..selector import KeyResult, Outcome, SelectorModel

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_REV = "\x1b[7m"
_BOLD = "\x1b[1m"
_ACCENT = "\x1b[36m"
_WARN = "\x1b[33m"
_GREEN = "\x1b[32m"


def _truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    if width <= 1:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _search_text(row: TV.Row) -> str:
    return f"{row.content} {row.label or ''} {TV.entry_kind(row.entry)}".lower()


def _matches(row: TV.Row, query: str) -> bool:
    toks = query.lower().split()
    if not toks:
        return True
    text = _search_text(row)
    return all(t in text for t in toks)


class SessionTreeModel(SelectorModel):
    def __init__(self, entries: list[T.Entry], leaf_id: str | None, mode: TV.FilterMode,
                 *, status: str = "", query: str = "", folded: set[str] | None = None) -> None:
        self.entries = entries
        self.leaf_id = leaf_id
        self.mode: TV.FilterMode = mode
        self.status = status
        self._query = query
        self.folded: set[str] = set(folded or ())
        self._base_count = 0
        self._rows: list[TV.Row] = []
        self._recompute()

    def _recompute(self) -> None:
        rows = TV.build_rows(self.entries, self.leaf_id, self.mode, self.folded)
        self._base_count = len(rows)
        q = self._query.strip()
        self._rows = [r for r in rows if _matches(r, q)] if q else rows

    # ── chrome ───────────────────────────────────────────────
    def max_visible(self, height: int) -> int:
        return max(5, height // 2)  # Pi tree-selector.ts:1169

    def header_lines(self, width: int) -> list[Any]:
        sid = self.entries[0].sessionId[-8:] if self.entries else "?"
        leaf = f" · leaf …{self.leaf_id[-8:]}" if self.leaf_id else ""
        title = f"  {_BOLD}Session Tree{_RESET}{_DIM} · {sid}{leaf} · filter: {self.mode}{_RESET}"
        help1 = (f"  {_DIM}↑↓ move · enter checkout · Ctrl+←/→ fold · Shift+L label · "
                 f"Ctrl+D/T/U/L/A filters · Ctrl+O cycle · type to search · q/esc{_RESET}")
        status = f"  {_ACCENT}{self.status}{_RESET}" if self.status else help1
        return [ANSI(title), ANSI(status)]

    def search_line(self, width: int) -> Any:
        return ANSI(f"  {_DIM}Type to search:{_RESET} {self._query}    "
                    f"{_DIM}{len(self._rows)}/{self._base_count}{_RESET}")

    def items(self) -> list:
        return self._rows

    def list_text(self, item: TV.Row, selected: bool, width: int) -> Any:
        prefix = item.prefix
        marker = "⊞ " if (item.folded and "⊞" not in prefix) else ("• " if item.on_active_path else "")
        lbl = f"[{item.label}] " if item.label else ""
        leaf = "  ◀" if item.is_leaf else ""
        cursor = "› " if selected else "  "
        avail = max(12, width - len(cursor) - len(prefix) - len(marker) - len(lbl) - len(leaf) - 2)
        content = _truncate(item.content, avail)
        if selected:
            line = f"{cursor}{prefix}{marker}{lbl}{content}{leaf}"
            return ANSI(f"{_REV}{line[:width]:<{min(width, len(line) + 1)}}{_RESET}")
        if content.startswith("user:"):
            head, _, rest = content.partition(":")
            shown = f"{_ACCENT}{head}:{_RESET}{rest}"
        elif content.startswith("assistant:"):
            head, _, rest = content.partition(":")
            shown = f"{_GREEN}{head}:{_RESET}{rest}"
        else:
            shown = f"{_DIM}{content}{_RESET}"
        mk = f"{_ACCENT}{marker}{_RESET}" if marker else ""
        lblc = f"{_WARN}{lbl}{_RESET}" if lbl else ""
        return ANSI(f"{cursor}{_DIM}{prefix}{_RESET}{mk}{lblc}{shown}{_ACCENT}{leaf}{_RESET}")

    # ── live search ──────────────────────────────────────────
    def supports_query(self) -> bool:
        return True

    def query(self) -> str:
        return self._query

    def set_query(self, query: str) -> None:
        self._query = query
        self.status = ""
        self._recompute()

    # ── keys ─────────────────────────────────────────────────
    def extra_keys(self) -> tuple[str, ...]:
        return ("c-d", "c-t", "c-u", "c-l", "c-a", "c-o", "c-left", "c-right", "L")

    def _set_mode(self, mode: TV.FilterMode, label: str) -> KeyResult:
        self.mode = mode
        self.status = label
        self._recompute()
        return KeyResult("refresh")

    def on_key(self, key: str, item: TV.Row, index: int) -> KeyResult | None:
        if key == "c-d":
            return self._set_mode("default", "filter: default")
        if key == "c-t":
            return self._set_mode("default" if self.mode == "no-tools" else "no-tools",
                                  "filter: " + ("default" if self.mode == "no-tools" else "no-tools"))
        if key == "c-u":
            return self._set_mode("default" if self.mode == "user-only" else "user-only",
                                  "filter: " + ("default" if self.mode == "user-only" else "user-only"))
        if key == "c-l":
            return self._set_mode("default" if self.mode == "labeled-only" else "labeled-only",
                                  "filter: " + ("default" if self.mode == "labeled-only" else "labeled-only"))
        if key == "c-a":
            return self._set_mode("default" if self.mode == "all" else "all",
                                  "filter: " + ("default" if self.mode == "all" else "all"))
        if key == "c-o":
            order = TV.FILTER_ORDER
            return self._set_mode(order[(order.index(self.mode) + 1) % len(order)], "")
        if key == "c-left" and item is not None and item.foldable and not item.folded:
            self.folded.add(item.entry.id)
            self.status = "folded"
            self._recompute()
            return KeyResult("refresh")
        if key == "c-right" and item is not None and item.folded:
            self.folded.discard(item.entry.id)
            self.status = "unfolded"
            self._recompute()
            return KeyResult("refresh")
        if key == "L" and item is not None:
            return KeyResult("edit", edit_action="label")
        return None


async def run_tree(manager, *, host) -> dict | None:
    """Run the tree page; returns ``{"action": "checkout", "entry_id": id}`` or None."""

    mode: TV.FilterMode = "default"
    index = 0
    status = ""
    query = ""
    folded: set[str] = set()
    while True:
        entries = manager.entries()
        leaf = manager.get_leaf()
        model = SessionTreeModel(entries, leaf, mode, status=status, query=query, folded=folded)
        status = ""
        outcome: Outcome = await host.run_selector(model, initial_index=index)
        mode, query, folded = model.mode, model.query(), model.folded
        index = outcome.index
        if outcome.kind == "cancel":
            return None
        if outcome.kind == "done":
            return {"action": "checkout", "entry_id": outcome.item.entry.id}
        if outcome.kind == "edit" and outcome.edit_action == "label":
            entry = outcome.item.entry
            cur = T.labels_by_id(entries).get(entry.id, "")
            text = await host.ask_text(f"label for …{entry.id[-8:]} (blank=clear) [{cur}]: ")
            if text is not None:
                manager.append_label(entry.id, text.strip())
                status = "label updated" if text.strip() else "label cleared"
            continue
