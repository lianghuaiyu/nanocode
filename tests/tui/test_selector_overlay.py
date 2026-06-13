"""tests/tui/test_selector_overlay.py —— in-app 选择器 overlay + ask_text（docs/18 step 4）。

驱动真 prompt_toolkit Application（pipe + DummyOutput）跑 TuiApp.run_selector / ask_text，验导航/
选中/取消/extra-key refresh/编辑、以及 ask_text 提交与取消。ptk 键：j=down、\r=Enter、q/esc 取消。
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nanocode.entrypoints.interactive.selector import KeyResult, SelectorModel
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
