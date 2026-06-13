"""entrypoints/interactive/tree_select.py — /tree 与 /fork 的交互选择器接线。

用 treemodel 的 build_rows 出树形 Row,套 selector 外壳:
  /tree :  ↑↓ 移动 · enter checkout · l 打/改 label · f 切 filter · q/esc 退出
  /fork :  只列 user 消息 · enter fork(复制到该消息之前+prompt 回填) · q/esc 退出
label 编辑需文本输入——selector 退出为 EDIT,本层经 ask_text 读一行后写 manager.append_label,再重跑。
仅在 TTY 下调用;非 TTY 文本回退在 builtin handler 里(用 treemodel.render_tree_text)。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from prompt_toolkit.formatted_text import ANSI

from ...session import tree as T
from . import treemodel as TM
from .selector import KeyResult, Outcome, SelectorModel, run_selector

# ANSI 颜色(轻量,不引 theme):dim 前缀 / accent 光标与 active / warning label
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_ACCENT = "\x1b[36m"
_WARN = "\x1b[33m"
_GREEN = "\x1b[32m"


class _TreeModel(SelectorModel):
    def __init__(self, entries: list[T.Entry], leaf_id: str | None, mode: TM.FilterMode,
                 fork_mode: bool) -> None:
        self.entries = entries
        self.leaf_id = leaf_id
        self.mode: TM.FilterMode = "user-only" if fork_mode else mode
        self.fork_mode = fork_mode
        self._rows: list[TM.Row] = []
        self._recompute()

    def _recompute(self) -> None:
        self._rows = TM.build_rows(self.entries, self.leaf_id, self.mode)

    # SelectorModel 钩子 ------------------------------------------------------
    def title(self) -> str:
        sid = self.entries[0].sessionId[-8:] if self.entries else "?"
        what = "Fork from user message" if self.fork_mode else "Session tree"
        return f"{_ACCENT}{what}{_RESET} · {sid}    {_DIM}(filter: {self.mode}){_RESET}"

    def items(self) -> list:
        return self._rows

    def list_text(self, item: TM.Row, selected: bool, width: int) -> Any:
        r = item
        cursor = f"{_ACCENT}› {_RESET}" if selected else "  "
        marker = f"{_ACCENT}• {_RESET}" if r.on_active_path else "  "
        lbl = f"{_WARN}[{r.label}] {_RESET}" if r.label else ""
        role_color = _ACCENT if r.content.startswith("user:") else _GREEN
        content = r.content
        if ":" in content and (content.startswith("user:") or content.startswith("assistant:")):
            head, _, rest = content.partition(":")
            content = f"{role_color}{head}:{_RESET}{rest}"
        cur = f"  {_ACCENT}◀{_RESET}" if r.is_leaf else ""
        return ANSI(f"{cursor}{_DIM}{r.prefix}{_RESET}{marker}{lbl}{content}{cur}")

    def preview_text(self, item: TM.Row, width: int) -> list[str]:
        e = item.entry
        head = f"{e.type} · …{e.id[-8:]}"
        if self.fork_mode:
            head = f"Prefill → 编辑器 · …{e.id[-8:]}"
        body = _full_text(e)
        lines = [head, ""]
        lines += _wrap(body, width)
        if item.label:
            lines += ["", f"⟨label: {item.label}⟩"]
        if self.fork_mode:
            lines += ["", "↳ 新 session: 复制到此消息之前 · prompt 回填编辑器"]
        return lines

    def hint(self) -> str:
        if self.fork_mode:
            return f"{_DIM}↑↓ move · enter fork · q/esc cancel{_RESET}"
        return f"{_DIM}↑↓ move · enter checkout · l label · f filter · q/esc{_RESET}"

    def extra_keys(self) -> tuple[str, ...]:
        return () if self.fork_mode else ("l", "f")

    def on_key(self, key: str, item: TM.Row, index: int) -> KeyResult | None:
        if key == "f" and not self.fork_mode:
            order = TM.FILTER_ORDER
            self.mode = order[(order.index(self.mode) + 1) % len(order)]
            self._recompute()
            return KeyResult("refresh")
        if key == "l" and not self.fork_mode and item is not None:
            return KeyResult("edit", edit_action="label")
        return None


def _full_text(e: T.Entry) -> str:
    if e.type == T.MESSAGE:
        msg = e.data.get("message") or {}
        return TM._extract_text(msg.get("content")) or TM.entry_content(e)
    return TM.entry_content(e)


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    text = (text or "").strip()
    if not text:
        return ["(empty)"]
    out: list[str] = []
    for para in text.splitlines() or [text]:
        out.extend(textwrap.wrap(para, max(20, width)) or [""])
    return out


async def run_tree(manager, *, ask_text: Callable[[str], Awaitable[str | None]],
                   fork_mode: bool = False) -> dict | None:
    """跑 /tree 或 /fork 交互循环。返回:
       {"action":"checkout","entry_id":id} / {"action":"fork","entry_id":id} / None(取消)。
    label 编辑在此就地写 manager.append_label 后重跑(保持光标+filter)。"""
    mode: TM.FilterMode = "default"
    index = 0
    while True:
        entries = manager.entries()
        leaf = manager.get_leaf()
        model = _TreeModel(entries, leaf, mode, fork_mode)
        outcome: Outcome = await run_selector(model, initial_index=index)
        mode = model.mode
        index = outcome.index
        if outcome.kind == "cancel":
            return None
        if outcome.kind == "done":
            e = outcome.item.entry
            return {"action": "fork" if fork_mode else "checkout", "entry_id": e.id}
        if outcome.kind == "edit" and outcome.edit_action == "label":
            e = outcome.item.entry
            cur = T.labels_by_id(entries).get(e.id, "")
            text = await ask_text(f"label for …{e.id[-8:]} (blank=clear) [{cur}]: ")
            if text is not None:
                manager.append_label(e.id, text.strip())
            continue
