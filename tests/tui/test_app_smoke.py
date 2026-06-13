"""tests/tui/test_app_smoke.py —— TuiApp 交互 app 冒烟（docs/18 step 2）。

用 `create_pipe_input` + `DummyOutput` + 假 RuntimeThread 驱动真 prompt_toolkit Application
（非 pty——纯事件循环冒烟）。验：bind_thread hydrate+订阅+边界、Enter 提交触发 thread.run、
事件归约进 state、运行中 Ctrl-C 调 abort、Ctrl-D 退出。abort 的**真 TTY** 验在 step 7（pty）。

ptk key 映射（已实测）：`\r`→Enter（提交）、`\n`→Ctrl-J（换行）、`\x03`→Ctrl-C、`\x04`→Ctrl-D。
"""

from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nanocode.agent import events as E
from nanocode.tui.app import TuiApp
from nanocode.tui.state import AssistantItem, SessionBoundaryItem, ToolItem


def _env(event):
    return {"thread_id": "t", "session_id": "s", "seq": 0, "type": event.kind, "event": event}


class FakeThread:
    """最小 RuntimeThread 替身：status/state/subscribe/run/abort + 事件 emit。"""

    def __init__(self, *, block: bool = False):
        self._listeners = []
        self.is_processing = False
        self.session_id = "fake123"
        self.run_calls = []
        self.abort_calls = 0
        self._block = block
        self._abort_event: asyncio.Event | None = None

    def status(self):
        return {
            "session_id": self.session_id, "cwd": "/repo", "session_name": "Fake",
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "context_window": 200000, "model": "fake-model", "thinking": None,
        }

    def state(self):
        s = self.status()
        s["is_processing"] = self.is_processing
        s["messages"] = []
        return s

    def subscribe(self, listener):
        self._listeners.append(listener)

        def unsub():
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsub

    def emit(self, env):
        for l in list(self._listeners):
            l(env)

    async def run(self, prompt):
        self.run_calls.append(prompt)
        self.is_processing = True
        self.emit(_env(E.LlmRequestPrepared(model="fake-model", message_count=1, messages_chars=10)))
        if self._block:
            self._abort_event = asyncio.Event()
            try:
                await self._abort_event.wait()
            finally:
                self.is_processing = False
            self.emit(_env(E.TurnAborted(input_tokens=1, output_tokens=0, turns=1)))
            return
        self.emit(_env(E.AssistantDelta(text="hi there")))
        self.emit(_env(E.AssistantMessageCompleted(
            message={}, text="hi there", thinking="", tool_uses=[], stop_reason="end", usage=None, latency_ms=5)))
        self.is_processing = False
        self.emit(_env(E.TurnCompleted(input_tokens=120, output_tokens=20, turns=1, cost_usd=0.004)))

    def abort(self):
        self.abort_calls += 1
        if self._abort_event is not None:
            self._abort_event.set()


# ─── bind_thread（无运行 loop，on_event 直接应用）──────────────────────────────


def test_bind_thread_hydrates_subscribes_and_marks_boundary():
    app = TuiApp(output=DummyOutput())
    thread = FakeThread()
    app.bind_thread(thread)
    assert app.state.status.model == "fake-model"
    assert app.state.status.session_name == "Fake"
    assert any(isinstance(i, SessionBoundaryItem) for i in app.state.timeline)
    assert len(thread._listeners) == 1  # subscribed
    # 直接 emit（loop=None → 同步应用）应归约进 state
    thread.emit(_env(E.UserMessageAccepted(text="hello")))
    assert any(getattr(i, "text", "") == "hello" for i in app.state.timeline)


def test_rebind_unsubscribes_old_thread():
    app = TuiApp(output=DummyOutput())
    t1 = FakeThread()
    t2 = FakeThread()
    app.bind_thread(t1)
    app.bind_thread(t2)
    assert len(t1._listeners) == 0  # old unsubscribed
    assert len(t2._listeners) == 1


# ─── 提交一轮（pipe 驱动 run_async）───────────────────────────────────────────


def test_submit_runs_turn_and_reduces_events():
    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput())
            thread = FakeThread()
            app.bind_thread(thread)
            task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            pipe.send_text("hello\r")          # 输入 + Enter 提交
            await asyncio.sleep(0.1)            # 让 thread.run 跑完并 emit
            pipe.send_text("\x04")             # Ctrl-D 退出
            await asyncio.wait_for(task, timeout=3)
            return app, thread

    app, thread = asyncio.run(scenario())
    assert thread.run_calls == ["hello"]
    assert any(isinstance(i, AssistantItem) and i.text == "hi there" for i in app.state.timeline)
    assert app.state.status.input_tokens == 120 and app.state.status.cost_usd == 0.004
    assert app.state.mode == "idle"


def test_ctrl_c_while_running_aborts_turn_without_exiting():
    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput())
            thread = FakeThread(block=True)   # run() 挂起到 abort
            app.bind_thread(thread)
            task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            pipe.send_text("do work\r")        # 提交 → thread.run 挂起
            await asyncio.sleep(0.08)
            running_mid = app._is_running()
            pipe.send_text("\x03")             # Ctrl-C → abort（不退出）
            await asyncio.sleep(0.08)
            aborted = thread.abort_calls
            alive = not task.done()            # app 仍在跑
            pipe.send_text("\x04")             # 现在才 Ctrl-D 退出
            await asyncio.wait_for(task, timeout=3)
            return running_mid, aborted, alive, app

    running_mid, aborted, alive, app = asyncio.run(scenario())
    assert running_mid is True
    assert aborted == 1
    assert alive is True               # Ctrl-C 没退出 app
    assert app.state.mode == "idle"    # turn_aborted 收回 idle


def test_ctrl_d_on_empty_buffer_exits():
    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput())
            app.bind_thread(FakeThread())
            task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            pipe.send_text("\x04")
            await asyncio.wait_for(task, timeout=3)
            return True

    assert asyncio.run(scenario()) is True


def test_render_event_invoked_for_each_event():
    """回归守卫：app 必须把每条事件转给注入的 render_event（transcript 渲染）。

    docs/18 fix：早期 smoke 从不设 render_event，导致「transcript 不渲染」的 bug（patch_stdout
    被 app 重绘覆盖）漏网。loop 未起时 _emit_above 直跑 fn，故可同步断言 render_event 被调。"""
    seen = []
    app = TuiApp(output=DummyOutput(), render_event=lambda env: seen.append(env.get("type")))
    app.bind_thread(FakeThread())
    for ev in (E.UserMessageAccepted(text="hi"),
               E.AssistantDelta(text="yo"),
               E.TurnCompleted(input_tokens=1, output_tokens=1, turns=1)):
        app.on_event(_env(ev))   # loop=None → _apply 同步、_emit_above 直跑
    assert seen == ["user_message_accepted", "assistant_delta", "turn_completed"]

    """docs/18 step 6：提交的行记入历史，↑（首行）回溯——cutover 平价回归守卫。"""
    from prompt_toolkit.history import InMemoryHistory

    async def scenario():
        with create_pipe_input() as pipe:
            app = TuiApp(input=pipe, output=DummyOutput(), history=InMemoryHistory())
            app.bind_thread(FakeThread())
            task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.05)
            pipe.send_text("/cost\r")            # 提交（记入历史）
            await asyncio.sleep(0.1)
            pipe.send_text("\x1b[A")             # ↑ 回溯
            await asyncio.sleep(0.1)
            recalled = app.input_buffer.text
            app.request_exit()
            try:
                await asyncio.wait_for(task, timeout=3)
            except Exception:
                pass
            return recalled

    assert asyncio.run(scenario()) == "/cost"

