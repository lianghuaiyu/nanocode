"""tests/tui/test_rich_app_smoke.py —— RichApp（Rich Live 客户端）冒烟。

用 os.pipe 当输入 seam（loop.add_reader 读，测试写字节）+ StringIO Console（force_terminal）
驱动真 Live，无真 TTY。验：提交触发 thread.run、事件归约、Ctrl-C abort 不退、Ctrl-D 退。
"""

from __future__ import annotations

import asyncio
import io
import os

from rich.console import Console

from nanocode.agent import events as E
from nanocode.tui.rich_app import RichApp


def _env(event):
    return {"thread_id": "t", "session_id": "s", "seq": 0, "type": event.kind, "event": event}


class FakeThread:
    def __init__(self, *, block: bool = False):
        self._listeners = []
        self.is_processing = False
        self.session_id = "fake123"
        self.run_calls = []
        self.abort_calls = 0
        self._block = block
        self._abort_event: asyncio.Event | None = None

    def status(self):
        return {"session_id": "fake123", "cwd": "/repo", "session_name": "Fake", "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0, "context_window": 200000, "model": "m", "thinking": None}

    def state(self):
        s = self.status(); s["is_processing"] = self.is_processing; s["messages"] = []
        return s

    def subscribe(self, l):
        self._listeners.append(l)
        return lambda: self._listeners.remove(l) if l in self._listeners else None

    def emit(self, env):
        for l in list(self._listeners):
            l(env)

    async def run(self, prompt):
        self.run_calls.append(prompt)
        self.is_processing = True
        self.emit(_env(E.LlmRequestPrepared(model="m", message_count=1, messages_chars=1)))
        if self._block:
            self._abort_event = asyncio.Event()
            try:
                await self._abort_event.wait()
            finally:
                self.is_processing = False
            self.emit(_env(E.TurnAborted(input_tokens=1, output_tokens=0, turns=1)))
            return
        self.emit(_env(E.AssistantDelta(text="hi")))
        self.emit(_env(E.AssistantMessageCompleted(message={}, text="hi", thinking="", tool_uses=[],
                                                    stop_reason="end", usage=None, latency_ms=1)))
        self.is_processing = False
        self.emit(_env(E.TurnCompleted(input_tokens=10, output_tokens=2, turns=1, cost_usd=0.001)))

    def abort(self):
        self.abort_calls += 1
        if self._abort_event is not None:
            self._abort_event.set()


def _console():
    return Console(file=io.StringIO(), force_terminal=True, width=100)


def test_submit_runs_turn_and_reduces():
    async def scenario():
        r, w = os.pipe()
        app = RichApp(input=r, output=_console())
        thread = FakeThread()
        app.bind_thread(thread)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        os.write(w, "hello\r".encode())
        await asyncio.sleep(0.15)
        os.write(w, b"\x04")            # Ctrl-D exit (editor empty after submit)
        await asyncio.wait_for(task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return app, thread

    app, thread = asyncio.run(scenario())
    assert thread.run_calls == ["hello"]
    from nanocode.tui.state import AssistantItem
    assert any(isinstance(i, AssistantItem) and i.text == "hi" for i in app.state.timeline)
    assert app.state.status.input_tokens == 10


def test_ctrl_c_aborts_running_turn_without_exit():
    async def scenario():
        r, w = os.pipe()
        app = RichApp(input=r, output=_console())
        thread = FakeThread(block=True)
        app.bind_thread(thread)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        os.write(w, "work\r".encode())
        await asyncio.sleep(0.1)
        running = app._is_running()
        os.write(w, b"\x03")           # Ctrl-C → abort
        await asyncio.sleep(0.1)
        aborted = thread.abort_calls
        alive = not task.done()
        os.write(w, b"\x04")           # now exit
        await asyncio.wait_for(task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return running, aborted, alive, app

    running, aborted, alive, app = asyncio.run(scenario())
    assert running is True
    assert aborted == 1
    assert alive is True
    assert app.state.mode == "idle"


def test_chinese_input_then_submit():
    async def scenario():
        r, w = os.pipe()
        app = RichApp(input=r, output=_console())
        thread = FakeThread()
        app.bind_thread(thread)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        os.write(w, "你好\r".encode("utf-8"))   # 多字节中文
        await asyncio.sleep(0.15)
        os.write(w, b"\x04")
        await asyncio.wait_for(task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return thread

    thread = asyncio.run(scenario())
    assert thread.run_calls == ["你好"]
