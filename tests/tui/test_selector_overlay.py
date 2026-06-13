"""tests/tui/test_selector_overlay.py —— in-app 选择器 overlay + ask_text（docs/18 step 4）。

驱动真 prompt_toolkit Application（pipe + DummyOutput）跑 TuiApp.run_selector / ask_text，验导航/
选中/取消/extra-key refresh/编辑、以及 ask_text 提交与取消。ptk 键：j=down、\r=Enter、q/esc 取消。
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nanocode.tui.selector import KeyResult, SelectorModel
from nanocode.tui.app import TuiApp


class _FakeThread:
    is_processing = False
    session_id = "fake"
    def status(self):
        return {"session_id": "fake", "cwd": "/r", "session_name": "F", "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0, "context_window": 200000, "model": "m", "thinking": None}
    def state(self):
        s = self.status(); s["is_processing"] = False; s["messages"] = []; return s
    def subscribe(self, l): return lambda: None


class _FakeModel(SelectorModel):
    def __init__(self):
        self._items = ["alpha", "beta", "gamma"]
        self.refreshed = 0
    def title(self): return "Pick one"
    def items(self): return self._items
    def list_text(self, item, selected, width): return ("> " if selected else "  ") + item
    def preview_text(self, item, width): return [f"preview of {item}"]
    def hint(self): return "enter pick · f refresh · q cancel"
    def extra_keys(self): return ("f", "l")
    def on_key(self, key, item, index):
        if key == "f":
            self.refreshed += 1
            return KeyResult("refresh")
        if key == "l":
            return KeyResult("edit", edit_action="label")
        return None


class _SearchModel(_FakeModel):
    def __init__(self):
        super().__init__()
        self._query = ""

    def supports_query(self): return True
    def query(self): return self._query
    def set_query(self, query):
        self._query = query
        self._items = [x for x in ["alpha", "beta", "gamma"] if query.lower() in x]
    def status_text(self): return f"{len(self._items)}"


class _ClipboardModel(_FakeModel):
    def extra_keys(self): return ("c",)
    def on_key(self, key, item, index):
        if key == "c":
            return KeyResult("refresh", clipboard_text=f"id:{item}")
        return None


class _SlashSearchModel(_FakeModel):
    def __init__(self):
        super().__init__()
        self.active = False
        self._query = ""

    def extra_keys(self): return ("/",) if self.active else ("/", "f")
    def supports_query(self): return self.active
    def query(self): return self._query
    def set_query(self, query): self._query = query
    def on_key(self, key, item, index):
        if key == "/":
            self.active = not self.active
            return KeyResult("refresh")
        if key == "f":
            return KeyResult("refresh")
        return None


def _drive(keys_after_open, *, pre_open=None):
    """开 run_selector，发送 keys_after_open（按序，每个后让出循环），返回 Outcome。"""
    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput())
            app.bind_thread(_FakeThread())
            model = _FakeModel()
            run_task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            sel_task = asyncio.create_task(app.run_selector(model))
            await asyncio.sleep(0.05)
            for k in keys_after_open:
                pipe.send_text(k)
                await asyncio.sleep(0.05)
            outcome = await asyncio.wait_for(sel_task, timeout=3)
            pipe.send_text("\x04")  # Ctrl-D 退 app
            await asyncio.wait_for(run_task, timeout=3)
            return outcome, model
    return asyncio.run(scenario())


def _drive_model(model, keys_after_open):
    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput())
            app.bind_thread(_FakeThread())
            run_task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            sel_task = asyncio.create_task(app.run_selector(model))
            await asyncio.sleep(0.05)
            for k in keys_after_open:
                pipe.send_text(k)
                await asyncio.sleep(0.05)
            outcome = await asyncio.wait_for(sel_task, timeout=3)
            pipe.send_text("\x04")
            await asyncio.wait_for(run_task, timeout=3)
            return outcome, model
    return asyncio.run(scenario())


def _drive_clipboard(model, keys_after_open):
    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput())
            app.bind_thread(_FakeThread())
            run_task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            sel_task = asyncio.create_task(app.run_selector(model))
            await asyncio.sleep(0.05)
            for k in keys_after_open:
                pipe.send_text(k)
                await asyncio.sleep(0.05)
            copied = app._app.clipboard.get_data().text
            pipe.send_text("q")
            outcome = await asyncio.wait_for(sel_task, timeout=3)
            pipe.send_text("\x04")
            await asyncio.wait_for(run_task, timeout=3)
            return outcome, copied
    return asyncio.run(scenario())


def test_navigate_and_select():
    outcome, _ = _drive(["j", "\r"])     # down → beta, enter
    assert outcome.kind == "done"
    assert outcome.item == "beta"
    assert outcome.index == 1


def test_enter_selects_first_by_default():
    outcome, _ = _drive(["\r"])
    assert outcome.kind == "done" and outcome.item == "alpha"


def test_q_cancels():
    outcome, _ = _drive(["q"])
    assert outcome.kind == "cancel"


def test_extra_key_refresh_stays_open_then_cancel():
    outcome, model = _drive(["f", "f", "q"])   # f refresh ×2（不退出）→ q cancel
    assert model.refreshed == 2
    assert outcome.kind == "cancel"


def test_extra_key_edit_returns_edit_outcome():
    outcome, _ = _drive(["l"])     # l → KeyResult("edit") → Outcome("edit", label)
    assert outcome.kind == "edit"
    assert outcome.edit_action == "label"


def test_selector_query_input_and_backspace():
    outcome, model = _drive_model(_SearchModel(), ["a", "r", "\x7f", "\r"])
    assert model.query() == "a"
    assert outcome.kind == "done"
    assert outcome.item == "alpha"


def test_selector_extra_key_can_write_clipboard():
    outcome, copied = _drive_clipboard(_ClipboardModel(), ["c"])
    assert outcome.kind == "cancel"
    assert copied == "id:alpha"


def test_static_selector_key_becomes_query_when_not_extra_key():
    outcome, model = _drive_model(_SlashSearchModel(), ["/", "f", "\r"])
    assert outcome.kind == "done"
    assert model.query() == "f"


def test_query_mode_jkq_are_text_not_nav_or_cancel():
    # 回归：搜索态下 j/k/q 必须当文本（旧 bug：j/k 导航、q 取消，导致搜索框打不出这些字符）。
    # 只有 Esc 取消。三键全进 query 即证明未触发导航/取消（若 q 取消了，后续 j/k 无法继续输入）。
    outcome, model = _drive_model(_SearchModel(), ["q", "j", "k", "\x1b"])
    assert model.query() == "qjk"
    assert outcome.kind == "cancel"


def test_ask_text_submit_and_cancel():
    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput())
            app.bind_thread(_FakeThread())
            run_task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            # submit
            t1 = asyncio.create_task(app.ask_text("name? "))
            await asyncio.sleep(0.05)
            pipe.send_text("hello\r")
            r1 = await asyncio.wait_for(t1, timeout=3)
            # cancel via esc
            t2 = asyncio.create_task(app.ask_text("again? "))
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b")
            r2 = await asyncio.wait_for(t2, timeout=3)
            pipe.send_text("\x04")
            await asyncio.wait_for(run_task, timeout=3)
            return r1, r2
    r1, r2 = asyncio.run(scenario())
    assert r1 == "hello"
    assert r2 is None
