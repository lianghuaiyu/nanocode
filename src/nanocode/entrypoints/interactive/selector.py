"""entrypoints/interactive/selector.py — 通用 prompt_toolkit 全屏选择器外壳。

B+C 自适应:终端宽 ≥ WIDE_THRESHOLD 走左右分栏(列表|预览),更窄走上下堆叠(列表/预览块)。
只在 TTY 下运行——非 TTY 由调用方走文本回退(本模块不负责回退)。

owner 提供一个 `SelectorModel`:给出标题/当前项列表/每项列表行文本/预览行/底部提示,
并处理自定义键(f 过滤、l 打标、r 改名…)。导航键(↑↓/jk)由外壳处理。
退出语义:enter→DONE(item)、q/esc/C-c→CANCEL、owner 可返回 EDIT(交回调用方做文本输入后重跑)。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any, Literal

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText, merge_formatted_text, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension

WIDE_THRESHOLD = 90


@dataclass
class KeyResult:
    """owner.on_key 的返回:continue(吞键)/refresh(项变了重画)/done(选中退出)/cancel/edit(退出做文本输入)。"""
    kind: Literal["continue", "refresh", "done", "cancel", "edit"]
    result: Any = None
    edit_action: str | None = None


@dataclass
class Outcome:
    kind: Literal["done", "cancel", "edit"]
    item: Any = None
    edit_action: str | None = None
    index: int = 0          # 退出时的光标位置(供 owner 重跑选择器时恢复)


class SelectorModel:
    """owner 实现这些钩子;list_text/preview_text 收 width 以便自行截断。"""
    def title(self) -> str: return ""
    def items(self) -> list: return []
    def list_text(self, item: Any, selected: bool, width: int) -> AnyFormattedText: return ""
    def preview_text(self, item: Any, width: int) -> list[str]: return []
    def hint(self) -> str: return ""
    def on_key(self, key: str, item: Any, index: int) -> KeyResult | None: return None
    # 自定义键集合(传给 KeyBindings 动态绑定);导航/enter/退出键不必列。
    def extra_keys(self) -> tuple[str, ...]: return ()


def _terminal_width() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


async def run_selector(model: SelectorModel, *, initial_index: int = 0) -> Outcome:
    """跑选择器 Application,返回 Outcome。仅应在 TTY 下调用。"""
    state = {"index": initial_index}
    width = _terminal_width()
    wide = width >= WIDE_THRESHOLD
    list_w = (width // 2 - 2) if wide else (width - 2)
    preview_w = (width - list_w - 3) if wide else (width - 2)

    def _clamp() -> None:
        n = len(model.items())
        if n == 0:
            state["index"] = 0
        else:
            state["index"] = max(0, min(state["index"], n - 1))

    def _cur():
        items = model.items()
        return items[state["index"]] if items else None

    def list_fragments():
        items = model.items()
        _clamp()
        frags: list = []
        # 简单滚动窗口:选中项居中
        height = max(1, shutil.get_terminal_size((80, 24)).lines - 6)
        n = len(items)
        start = max(0, min(state["index"] - height // 2, max(0, n - height)))
        end = min(start + height, n)
        for i in range(start, end):
            sel = i == state["index"]
            frags.append(to_formatted_text(model.list_text(items[i], sel, list_w)))
            frags.append(to_formatted_text("\n"))
        return merge_formatted_text(frags) if frags else to_formatted_text("  (empty)")

    def preview_fragments():
        item = _cur()
        if item is None:
            return to_formatted_text("")
        return ANSI("\n".join(model.preview_text(item, preview_w)))

    def title_fragments():
        return ANSI(model.title())

    def hint_fragments():
        return ANSI(model.hint())

    list_win = Window(FormattedTextControl(list_fragments), wrap_lines=False,
                      width=Dimension(weight=1) if wide else None)
    preview_win = Window(FormattedTextControl(preview_fragments), wrap_lines=True,
                         width=Dimension(weight=1) if wide else None)
    body = (VSplit([list_win, Window(width=1, char="│"), preview_win]) if wide
            else HSplit([list_win, Window(height=1, char="─"), preview_win]))
    root = HSplit([
        Window(FormattedTextControl(title_fragments), height=1),
        Window(height=1, char="─"),
        body,
        Window(height=1, char="─"),
        Window(FormattedTextControl(hint_fragments), height=1),
    ])

    kb = KeyBindings()
    outcome: dict = {"val": Outcome("cancel")}

    @kb.add("up")
    @kb.add("k")
    @kb.add("c-p")
    def _up(event): state["index"] -= 1; _clamp()

    @kb.add("down")
    @kb.add("j")
    @kb.add("c-n")
    def _down(event): state["index"] += 1; _clamp()

    @kb.add("enter")
    def _enter(event):
        item = _cur()
        if item is not None:
            outcome["val"] = Outcome("done", item=item)
            event.app.exit()

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event):
        outcome["val"] = Outcome("cancel")
        event.app.exit()

    def _make_extra(key: str):
        def handler(event):
            r = model.on_key(key, _cur(), state["index"])
            if r is None or r.kind == "continue":
                return
            if r.kind == "refresh":
                _clamp(); return
            if r.kind == "done":
                outcome["val"] = Outcome("done", item=r.result if r.result is not None else _cur())
                event.app.exit()
            elif r.kind == "cancel":
                outcome["val"] = Outcome("cancel"); event.app.exit()
            elif r.kind == "edit":
                outcome["val"] = Outcome("edit", item=_cur(), edit_action=r.edit_action or key)
                event.app.exit()
        return handler

    for key in model.extra_keys():
        kb.add(key)(_make_extra(key))

    app = Application(layout=Layout(root), key_bindings=kb, full_screen=True, mouse_support=False)
    await app.run_async()
    final = outcome["val"]
    final.index = state["index"]
    return final
