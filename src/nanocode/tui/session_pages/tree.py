"""Session tree page — Pi single-column layout with fold/unfold + always-live search.

Mirrors Pi ``tree-selector.ts``: bordered full-width tree, ASCII connectors + fold glyphs
(⊟/⊞), active-path marker, Ctrl+O filter cycle, Ctrl/Alt+Left/Right branch navigation,
Shift+L label, Shift+T label timestamps, type-to-search (whitespace-AND substring).
No preview, no id column.
``/fork`` is a separate flat page (see ``fork.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


from ...session import tree as T
from ...session import tree_view as TV
from ..selector import KeyResult, Outcome, SelectorModel, cell_width, pad_cells, truncate_cells
from ..theme import BOLD as _BOLD, DIM as _DIM, RESET as _RESET, fg as _fg

_ACCENT = _fg("accent")
_WARN = _fg("warning")
_GREEN = _fg("success")
_ROW_PAD = "  "
_RIGHT_PAD = "  "
_LEAF_GAP = "  "
_LEAF_MARK = "◀ current"
_LEAF_COL_WIDTH = cell_width(_LEAF_MARK)


def _span(text: str, color: str = "", *, bold: bool = False) -> str:
    style = (_BOLD if bold else "") + color
    return f"{style}{text}{_RESET}" if style else text


def _message_role(item: TV.Row) -> str | None:
    if item.entry.type != T.MESSAGE:
        return None
    return (item.entry.data.get("message") or {}).get("role")


def _styled_content(item: TV.Row, content: str, *, bold: bool = False) -> str:
    role = _message_role(item)
    if role == "user":
        prefix = "user:"
        if content.startswith(prefix):
            return _span(prefix, _ACCENT, bold=bold) + _span(content[len(prefix):], bold=bold)
        return _span(content, _ACCENT, bold=bold)
    if role == "assistant":
        prefix = "assistant:"
        if content.startswith(prefix):
            return _span(prefix, _GREEN, bold=bold) + _span(content[len(prefix):], bold=bold)
        return _span(content, _GREEN, bold=bold)
    return _span(content, _DIM, bold=bold)


def _truncate(text: str, width: int) -> str:
    text = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in text).strip()
    return truncate_cells(text, width)


def _label_time(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
    except Exception:
        return timestamp[:16]
    return dt.strftime("%H:%M")


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
        self.show_label_timestamps = False
        self._query = query
        self.folded: set[str] = set(folded or ())
        self._base_count = 0
        self._rows: list[TV.Row] = []
        self._recompute()

    def _recompute(self) -> None:
        rows = TV.build_rows(self.entries, self.leaf_id, self.mode)
        self._base_count = len(rows)
        q = self._query.strip()
        rows = [r for r in rows if _matches(r, q)] if q else rows
        self._rows = self._apply_folded(rows)
        self._sync_visible_fold_state()

    def _apply_folded(self, rows: list[TV.Row]) -> list[TV.Row]:
        if not self.folded:
            return rows
        by_id = {e.id: e for e in self.entries}
        out: list[TV.Row] = []
        for row in rows:
            cur = row.entry.parentId
            hidden = False
            while cur is not None:
                if cur in self.folded:
                    hidden = True
                    break
                cur = by_id.get(cur).parentId if by_id.get(cur) is not None else None
            if not hidden:
                out.append(row)
        return out

    def _sync_visible_fold_state(self) -> None:
        if not self._rows:
            return
        parent, children, _ = self._visible_maps()
        for row in self._rows:
            entry_id = row.entry.id
            parent_id = parent.get(entry_id)
            siblings = children.get(parent_id, [])
            row.foldable = bool(children.get(entry_id)) and (parent_id is None or len(siblings) > 1)
            row.folded = entry_id in self.folded

    # ── chrome ───────────────────────────────────────────────
    def max_visible(self, height: int) -> int:
        return max(5, height // 2)  # Pi tree-selector.ts:1169

    def header_lines(self, width: int) -> list[Any]:
        title = f"  {_BOLD}Session Tree{_RESET}"
        help1 = (f"  {_DIM}↑/↓: move. ←/→: page. Ctrl+←/→ or Alt+←/→: fold/branch. "
                 f"Shift+L: label. Shift+T: label time. Ctrl+O: filter. type to search{_RESET}")
        if self.status:
            help1 = f"  {_ACCENT}{self.status}{_RESET}"
        return [title, help1]

    def search_line(self, width: int) -> str:
        if self._query:
            return f"  {_DIM}Type to search:{_RESET} {_ACCENT}{self._query}{_RESET}"
        return f"  {_DIM}Type to search:{_RESET}"

    def body_border_after_search(self) -> bool:
        return True

    def status_suffix(self) -> str:
        labels = {
            "no-tools": " [no-tools]",
            "user-only": " [user]",
            "labeled-only": " [labeled]",
            "all": " [all]",
        }
        return labels.get(self.mode, "")

    def position_line(self, index: int, total: int, visible_start: int, visible_end: int, width: int) -> str | None:
        shown = index + 1 if total else 0
        return f"  ({shown}/{total}){self.status_suffix()}"

    def empty_text(self, width: int) -> str:
        return "  No entries found"

    def items(self) -> list:
        return self._rows

    def initial_index(self) -> int:
        if not self._rows:
            return 0
        for i, row in enumerate(self._rows):
            if row.entry.id == self.leaf_id:
                return i
        active = [i for i, row in enumerate(self._rows) if row.on_active_path]
        return active[-1] if active else 0

    def list_text(self, item: TV.Row, selected: bool, width: int) -> Any:
        prefix = item.prefix
        marker = ("⊞ " if (item.folded and "⊞" not in prefix) else "") + ("• " if item.on_active_path else "")
        lbl = f"[{item.label}] " if item.label else ""
        label_time = f"{_label_time(item.label_timestamp)} " if self.show_label_timestamps and item.label else ""
        leaf = _LEAF_MARK if item.is_leaf else ""
        cursor = "› " if selected else "  "        # plain(供宽度计算)
        before = (cell_width(cursor) + cell_width(_ROW_PAD) + cell_width(prefix) + cell_width(marker)
                  + cell_width(lbl) + cell_width(label_time))
        show_leaf_col = width >= before + 1 + cell_width(_LEAF_GAP) + _LEAF_COL_WIDTH + cell_width(_RIGHT_PAD)
        after = (cell_width(_LEAF_GAP) + _LEAF_COL_WIDTH if show_leaf_col else 0) + cell_width(_RIGHT_PAD)
        avail = max(1, width - before - after)
        content = _truncate(item.content, avail)
        used = before + cell_width(content) + after
        leaf_gap = _LEAF_GAP + (" " * max(0, width - used)) if show_leaf_col else ""
        mk = f"{_ACCENT}{marker}{_RESET}" if marker else ""
        lblc = f"{_WARN}{lbl}{_RESET}" if lbl else ""
        ltime = f"{_DIM}{label_time}{_RESET}" if label_time else ""
        shown = _styled_content(item, content, bold=selected)
        leaf_col = pad_cells(leaf, _LEAF_COL_WIDTH) if show_leaf_col else ""
        leafc = _span(leaf_col, _ACCENT, bold=True) if leaf else leaf_col
        if selected:                               # Pi:accent '› ' 游标 + bold 行(无反显)
            cur = f"{_ACCENT}{cursor}{_RESET}"
            return f"{cur}{_ROW_PAD}{_DIM}{prefix}{_RESET}{mk}{lblc}{ltime}{shown}{leaf_gap}{leafc}{_RIGHT_PAD}"
        return f"{cursor}{_ROW_PAD}{_DIM}{prefix}{_RESET}{mk}{lblc}{ltime}{shown}{leaf_gap}{leafc}{_RIGHT_PAD}"

    # ── live search ──────────────────────────────────────────
    def supports_query(self) -> bool:
        return True

    def wrap_navigation(self) -> bool:
        return True

    def escape_clears_query(self) -> bool:
        return True

    def query(self) -> str:
        return self._query

    def set_query(self, query: str) -> None:
        self._query = query
        self.status = ""
        self.folded.clear()
        self._recompute()

    # ── keys ─────────────────────────────────────────────────
    def extra_keys(self) -> tuple[str, ...]:
        return ("c-o", "c-left", "c-right", "L", "T")

    def _set_mode(self, mode: TV.FilterMode, label: str, *, keep_id: str | None = None,
                  fallback: int = 0) -> KeyResult:
        self.mode = mode
        self.status = label
        self.folded.clear()
        self._recompute()
        return KeyResult("refresh", result=self._nearest_visible_index(keep_id, fallback))

    def on_key(self, key: str, item: TV.Row, index: int) -> KeyResult | None:
        if key == "c-o":
            order = TV.FILTER_ORDER
            mode = order[(order.index(self.mode) + 1) % len(order)]
            keep_id = item.entry.id if item is not None else None
            return self._set_mode(mode, f"filter: {mode}", keep_id=keep_id, fallback=index)
        if key == "c-left" and item is not None and item.foldable and not item.folded:
            self.folded.add(item.entry.id)
            self.status = "folded"
            self._recompute()
            return KeyResult("refresh", result=self._nearest_visible_index(item.entry.id, index))
        if key == "c-right" and item is not None and item.folded:
            self.folded.discard(item.entry.id)
            self.status = "unfolded"
            self._recompute()
            return KeyResult("refresh", result=self._nearest_visible_index(item.entry.id, index))
        if key == "c-left":
            return KeyResult("refresh", result=self._branch_segment_start(index, "up"))
        if key == "c-right":
            return KeyResult("refresh", result=self._branch_segment_start(index, "down"))
        if key == "L" and item is not None:
            return KeyResult("edit", edit_action="label")
        if key == "T":
            self.show_label_timestamps = not self.show_label_timestamps
            self.status = "label timestamps shown" if self.show_label_timestamps else "label timestamps hidden"
            return KeyResult("refresh")
        return None

    def _nearest_visible_index(self, entry_id: str | None, fallback: int = 0) -> int:
        if not self._rows:
            return 0
        if entry_id is None:
            return max(0, min(fallback, len(self._rows) - 1))
        by_row = {row.entry.id: i for i, row in enumerate(self._rows)}
        by_id = {e.id: e for e in self.entries}
        cur = entry_id
        while cur is not None:
            idx = by_row.get(cur)
            if idx is not None:
                return idx
            cur = by_id.get(cur).parentId if by_id.get(cur) is not None else None
        return max(0, min(fallback, len(self._rows) - 1))

    def _visible_maps(self) -> tuple[dict[str, str | None], dict[str | None, list[str]], dict[str, int]]:
        ids = [row.entry.id for row in self._rows]
        visible = set(ids)
        by_id = {e.id: e for e in self.entries}
        parent: dict[str, str | None] = {}
        children: dict[str | None, list[str]] = {None: []}
        for row in self._rows:
            cur = row.entry.parentId
            while cur is not None and cur not in visible:
                cur = by_id.get(cur).parentId if by_id.get(cur) is not None else None
            parent[row.entry.id] = cur
            children.setdefault(cur, []).append(row.entry.id)
        return parent, children, {entry_id: i for i, entry_id in enumerate(ids)}

    def _branch_segment_start(self, index: int, direction: str) -> int:
        if not self._rows:
            return 0
        index = max(0, min(index, len(self._rows) - 1))
        parent, children, by_id = self._visible_maps()
        current = self._rows[index].entry.id
        if direction == "down":
            while True:
                kids = children.get(current, [])
                if not kids:
                    return by_id[current]
                if len(kids) > 1:
                    return by_id[kids[0]]
                current = kids[0]
        while True:
            pid = parent.get(current)
            if pid is None:
                return by_id[current]
            siblings = children.get(pid, [])
            if len(siblings) > 1:
                segment = by_id[current]
                if segment < index:
                    return segment
            current = pid


async def run_tree(manager, *, host, set_label=None) -> dict | None:
    """Run the tree page; returns ``{"action": "checkout", "entry_id": id}`` or None."""

    mode: TV.FilterMode = "default"
    index: int | None = None
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
                if set_label is None:
                    status = "label operation unavailable"
                    continue
                set_label(entry.id, text.strip())
                status = "label updated" if text.strip() else "label cleared"
            continue
