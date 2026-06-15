"""tests/tui/test_rich_selector.py —— RichApp.run_selector / ask_text 经 os.pipe 字节驱动。

验导航(↑/j/k)/选中/取消(q,esc)/extra-key refresh/编辑/搜索态文本(含 j/k/q 当文本)/ask_text。
"""

from __future__ import annotations

import asyncio
import io
import os

from rich.console import Console

from nanocode.tui.rich_app import RichApp
from nanocode.tui.selector import KeyResult, SelectorModel


class _Thread:
    is_processing = False
    session_id = "f"
    def status(self):
        return {"session_id": "f", "cwd": "/r", "session_name": "F", "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0, "context_window": 200000, "model": "m", "thinking": None}
    def state(self):
        s = self.status(); s["is_processing"] = False; s["messages"] = []; return s
    def subscribe(self, l): return lambda: None


class _Model(SelectorModel):
    def __init__(self, *, query=False, wrap=False, escape_clears=False, initial=0, items=None):
        self._items = list(items or ["alpha", "beta", "gamma"])
        self.refreshed = 0
        self._q = ""
        self._query = query
        self._wrap = wrap
        self._escape_clears = escape_clears
        self._initial = initial
    def header_lines(self, w): return ["pick one"]
    def items(self): return self._items
    def initial_index(self): return self._initial
    def list_text(self, it, sel, w): return ("> " if sel else "  ") + it
    def extra_keys(self): return ("f", "l", "c-o")
    def supports_query(self): return self._query
    def query(self): return self._q
    def wrap_navigation(self): return self._wrap
    def escape_clears_query(self): return self._escape_clears
    def set_query(self, q):
        self._q = q
        base = ["alpha", "beta", "gamma"]
        self._items = [x for x in base if q.lower() in x] or base
    def on_key(self, k, it, i):
        if k == "f":
            self.refreshed += 1
            return KeyResult("refresh")
        if k == "l":
            return KeyResult("edit", edit_action="label")
        if k == "c-o":
            self.refreshed += 1
            return KeyResult("refresh")
        return None


def _drive_selector(model, sends: list[bytes]):
    async def scenario():
        r, w = os.pipe()
        app = RichApp(input=r, output=Console(file=io.StringIO(), force_terminal=True, width=100))
        app.bind_thread(_Thread())
        run_task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        sel_task = asyncio.create_task(app.run_selector(model))
        await asyncio.sleep(0.05)
        for b in sends:
            os.write(w, b)
            await asyncio.sleep(0.05)
        outcome = await asyncio.wait_for(sel_task, timeout=3)
        os.write(w, b"\x04")
        await asyncio.wait_for(run_task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return outcome, model
    return asyncio.run(scenario())


def test_navigate_and_select():
    outcome, _ = _drive_selector(_Model(), [b"j", b"\r"])   # down → beta
    assert outcome.kind == "done" and outcome.item == "beta" and outcome.index == 1


def test_arrow_down_navigates():
    outcome, _ = _drive_selector(_Model(), [b"\x1b[B", b"\r"])  # arrow-down → beta
    assert outcome.kind == "done" and outcome.item == "beta"


def test_selector_uses_model_initial_index():
    outcome, _ = _drive_selector(_Model(initial=2), [b"\r"])
    assert outcome.kind == "done" and outcome.item == "gamma" and outcome.index == 2


def test_mouse_wheel_down_navigates_selector():
    outcome, _ = _drive_selector(_Model(), [b"\x1b[<65;10;5M", b"\r"])
    assert outcome.kind == "done" and outcome.item == "gamma" and outcome.index == 2


def test_q_cancels():
    outcome, _ = _drive_selector(_Model(), [b"q"])
    assert outcome.kind == "cancel"


def test_extra_key_refresh_then_cancel():
    outcome, model = _drive_selector(_Model(), [b"f", b"f", b"q"])
    assert model.refreshed == 2 and outcome.kind == "cancel"


def test_extra_key_edit():
    outcome, _ = _drive_selector(_Model(), [b"l"])
    assert outcome.kind == "edit" and outcome.edit_action == "label"


def test_ctrl_letter_extra_key_is_normalized():
    outcome, model = _drive_selector(_Model(), [b"\x0f", b"q"])  # Ctrl+O refresh, q cancel
    assert model.refreshed == 1 and outcome.kind == "cancel"


def test_query_mode_filters_and_jkq_are_text():
    # 搜索态：j/k/q 当文本进 query（不导航/取消）；只 esc 取消
    outcome, model = _drive_selector(_Model(query=True), ["q".encode(), "j".encode(), "k".encode(), b"\x1b"])
    assert model.query() == "qjk"
    assert outcome.kind == "cancel"


def test_selector_wrap_navigation_hook():
    outcome, _ = _drive_selector(_Model(wrap=True), [b"\x1b[A", b"\r"])  # up from first wraps to gamma
    assert outcome.kind == "done" and outcome.item == "gamma" and outcome.index == 2


def test_selector_page_navigation_clamps_even_when_move_wraps():
    items = [f"item{i}" for i in range(10)]
    outcome, _ = _drive_selector(_Model(wrap=True, initial=8, items=items), [b"\x1b[C", b"\r"])
    assert outcome.kind == "done" and outcome.item == "item9" and outcome.index == 9


def test_selector_escape_can_clear_query_before_cancel():
    outcome, model = _drive_selector(_Model(query=True, escape_clears=True), [b"b", b"\x1b", b"\x1b"])
    assert model.query() == ""
    assert outcome.kind == "cancel"


def test_ask_text_submit_and_cancel():
    async def scenario():
        r, w = os.pipe()
        app = RichApp(input=r, output=Console(file=io.StringIO(), force_terminal=True, width=100))
        app.bind_thread(_Thread())
        run_task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        t1 = asyncio.create_task(app.ask_text("name? "))
        await asyncio.sleep(0.05)
        os.write(w, "hello\r".encode())
        r1 = await asyncio.wait_for(t1, timeout=3)
        t2 = asyncio.create_task(app.ask_text("again? "))
        await asyncio.sleep(0.05)
        os.write(w, b"\x1b")
        r2 = await asyncio.wait_for(t2, timeout=3)
        os.write(w, b"\x04")
        await asyncio.wait_for(run_task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return r1, r2
    r1, r2 = asyncio.run(scenario())
    assert r1 == "hello" and r2 is None
