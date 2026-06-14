"""Shared in-app selector protocol (Pi single-column bordered overlay).

Page owners (`session_pages/*`) provide a model; ``TuiApp`` owns the single
prompt_toolkit Application and renders the model as a **full-width, single-column,
bordered panel** (Pi `session-selector.ts` / `tree-selector.ts` layout — search/header
on top, list below, NO preview pane). No selector starts its own Application.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any, Literal

WIDE_THRESHOLD = 90


@dataclass
class KeyResult:
    """Result returned by ``SelectorModel.on_key``."""

    kind: Literal["continue", "refresh", "done", "cancel", "edit"]
    result: Any = None
    edit_action: str | None = None
    clipboard_text: str | None = None


@dataclass
class Outcome:
    """Final selector result returned to the page owner."""

    kind: Literal["done", "cancel", "edit"]
    item: Any = None
    edit_action: str | None = None
    index: int = 0


class SelectorModel:
    """Page-owned model consumed by ``TuiApp.run_selector`` (Pi single-column layout)."""

    # ── content ──────────────────────────────────────────────
    def items(self) -> list:
        return []

    def list_text(self, item: Any, selected: bool, width: int) -> str:
        """Render one full-width row (the app adds cursor margin + selected-row bg)."""
        return ""

    # ── header / chrome (model formats these Pi-faithfully; app frames in borders) ──
    def header_lines(self, width: int) -> list[str]:
        """Title + right-justified indicators + hint line(s); model owns confirm/status takeover."""
        return []

    def search_line(self, width: int) -> str | None:
        """The `Search: <q>` / `Type to search: <q>` line, or None for no search row."""
        return None

    def status_suffix(self) -> str:
        """Trailing label appended to the app's `(i/total)` indicator (e.g. ` [no-tools]`)."""
        return ""

    def max_visible(self, height: int) -> int:
        """How many list rows the panel shows (Pi: resume=10, tree=max(5, h//2))."""
        return max(5, height - 8)

    # ── interaction ──────────────────────────────────────────
    def confirming(self) -> bool:
        """True when a destructive confirm (e.g. delete) is pending — app routes only enter/esc."""
        return False

    def on_key(self, key: str, item: Any, index: int) -> KeyResult | None:
        return None

    def extra_keys(self) -> tuple[str, ...]:
        return ()

    # ── live search ──────────────────────────────────────────
    def supports_query(self) -> bool:
        return False

    def query(self) -> str:
        return ""

    def set_query(self, query: str) -> None:
        pass


def terminal_width() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80
