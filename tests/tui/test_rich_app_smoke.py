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
    def __init__(
        self,
        *,
        block: bool = False,
        emit_delta: bool = True,
        final_text: str = "hi",
        subagent_records: list[dict] | None = None,
    ):
        self._listeners = []
        self.is_processing = False
        self.session_id = "fake123"
        self.run_calls = []
        self.abort_calls = 0
        self._block = block
        self._abort_event: asyncio.Event | None = None
        self.transcript_messages = []
        self.emit_delta = emit_delta
        self.final_text = final_text
        self.subagent_records = list(subagent_records or [])

    def status(self):
        return {"session_id": "fake123", "cwd": "/repo", "session_name": "Fake", "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0, "context_window": 200000, "model": "m", "thinking": None}

    def state(self):
        s = self.status()
        s["is_processing"] = self.is_processing
        s["messages"] = []
        s["transcript_messages"] = self.transcript_messages
        return s

    def subagent_widget_snapshot(self):
        return list(self.subagent_records)

    def subscribe(self, l):
        self._listeners.append(l)
        return lambda: self._listeners.remove(l) if l in self._listeners else None

    def emit(self, env):
        for l in list(self._listeners):
            l(env)

    async def run(self, prompt):
        self.run_calls.append(prompt)
        self.is_processing = True
        self.emit(_env(E.UserMessageAccepted(text=prompt)))
        self.emit(_env(E.LlmRequestPrepared(model="m", message_count=1, messages_chars=1)))
        if self._block:
            self._abort_event = asyncio.Event()
            try:
                await self._abort_event.wait()
            finally:
                self.is_processing = False
            self.emit(_env(E.TurnAborted(input_tokens=1, output_tokens=0, turns=1)))
            return
        if self.emit_delta:
            self.emit(_env(E.AssistantDelta(text=self.final_text)))
        self.emit(_env(E.AssistantMessageCompleted(message={}, text=self.final_text, thinking="", tool_uses=[],
                                                    stop_reason="end", usage=None, latency_ms=1)))
        self.is_processing = False
        self.emit(_env(E.TurnCompleted(input_tokens=10, output_tokens=2, turns=1, cost_usd=0.001)))

    def abort(self):
        self.abort_calls += 1
        if self._abort_event is not None:
            self._abort_event.set()


def _console(*, height: int | None = None, width: int = 100):
    return Console(file=io.StringIO(), force_terminal=True, width=width, height=height)


def _render(app: RichApp, con: Console) -> str:
    con.file.seek(0)
    con.file.truncate(0)
    con.print(app)
    return con.file.getvalue()


def _running_subagent_record() -> dict:
    return {
        "status": "running",
        "description": "background sleep",
        "agent_type": "coder",
        "started_at": "2026-06-29T15:00:00Z",
        "metrics": {
            "toolUses": 1,
            "currentTool": "run_shell",
            "activeTools": [
                {
                    "tool": "run_shell",
                    "inputSummary": "{\"command\": \"sleep 600\"}",
                }
            ],
        },
    }


def test_submit_runs_turn_and_reduces():
    async def scenario():
        r, w = os.pipe()
        con = _console()
        app = RichApp(input=r, output=con)
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
        return app, thread, con

    app, thread, con = asyncio.run(scenario())
    assert thread.run_calls == ["hello"]
    assert "hi" in con.file.getvalue()
    assert all(type(i).__name__ != "AssistantItem" for i in app.state.timeline)
    assert app.state.status.input_tokens == 10


def test_bind_thread_clears_live_timeline_residue():
    from nanocode.tui.state import NoticeItem, SessionBoundaryItem

    app = RichApp(output=_console())
    app.bind_thread(FakeThread())
    app.state.timeline.append(NoticeItem(text="old session warning", level="warn"))

    app.bind_thread(FakeThread())

    assert not any(isinstance(i, NoticeItem) for i in app.state.timeline)
    assert any(isinstance(i, SessionBoundaryItem) for i in app.state.timeline)


def test_run_turn_surfaces_submit_exceptions():
    from nanocode.tui.state import ErrorItem

    async def scenario():
        con = _console()
        app = RichApp(output=con)
        app.bind_thread(FakeThread())

        async def submit(_text):
            raise RuntimeError("selector exploded")

        app.set_submit_handler(submit)
        app.state.mode = "running"
        await app._run_turn("boom")
        return app, con

    app, con = asyncio.run(scenario())

    assert app.state.mode == "idle"
    assert any(isinstance(i, ErrorItem) and i.text == "selector exploded" for i in app.state.timeline)
    assert "Error: selector exploded" in con.file.getvalue()


def test_session_switch_notice_commits_above_without_live_residue():
    from nanocode.tui.state import NoticeItem

    con = _console()
    app = RichApp(output=con)
    app.bind_thread(FakeThread())
    app.on_event(_env(E.NoticeRaised(text="Session → abc12345 (34 messages).")))

    out = con.file.getvalue()
    assert out.count("Session → abc12345 (34 messages).") == 1
    assert not any(isinstance(i, NoticeItem) and i.text.startswith("Session → ") for i in app.state.timeline)


def test_session_switch_notice_from_runtime_dict_does_not_enter_live_timeline():
    from nanocode.tui.state import NoticeItem

    con = _console()
    app = RichApp(output=con)
    app.bind_thread(FakeThread())
    app.on_event({
        "thread_id": "t",
        "session_id": "abc12345",
        "seq": 1,
        "type": "notice_raised",
        "event": {
            "text": "Session → abc12345 (34 messages).",
            "level": "info",
            "kind": "notice_raised",
        },
    })

    out = con.file.getvalue()
    assert out.count("Session → abc12345 (34 messages).") == 1
    assert not any(isinstance(i, NoticeItem) and i.text.startswith("Session → ") for i in app.state.timeline)


def test_background_subagent_terminal_notice_prints_once_without_live_residue():
    from nanocode.tui.state import NoticeItem

    con = _console()
    app = RichApp(output=con)
    app.bind_thread(FakeThread())
    text = "Background sub-agent run sess_abc completed: UI smoke"
    app.on_event(_env(E.NoticeRaised(text=text)))

    out = con.file.getvalue()
    assert out.count(text) == 1
    assert not any(isinstance(i, NoticeItem) and i.text == text for i in app.state.timeline)


def test_regular_notice_stays_in_live_timeline():
    from nanocode.tui.state import NoticeItem

    app = RichApp(output=_console())
    app.bind_thread(FakeThread())
    app.on_event(_env(E.NoticeRaised(text="heads up")))

    assert any(isinstance(i, NoticeItem) and i.text == "heads up" for i in app.state.timeline)


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


def test_background_widget_alone_does_not_need_periodic_refresh():
    async def scenario():
        app = RichApp(output=_console())
        app.bind_thread(FakeThread(subagent_records=[_running_subagent_record()]))

        background_only = app._needs_periodic_refresh()
        app._turn_task = asyncio.create_task(asyncio.sleep(10))
        try:
            foreground_turn = app._needs_periodic_refresh()
        finally:
            app._turn_task.cancel()
            try:
                await app._turn_task
            except asyncio.CancelledError:
                pass
            app._turn_task = None
        return background_only, foreground_turn

    background_only, foreground_turn = asyncio.run(scenario())
    assert background_only is False
    assert foreground_turn is True


def test_selector_suppresses_foreground_periodic_refresh():
    from nanocode.tui.selector import ChoiceItem, ChoiceModel
    from nanocode.tui.state import SelectorState

    async def scenario():
        app = RichApp(output=_console())
        app.bind_thread(FakeThread())
        app._turn_task = asyncio.create_task(asyncio.sleep(10))
        try:
            model = ChoiceModel(
                "Actions",
                [ChoiceItem(label="Open", value="open")],
            )
            app.state.selector = SelectorState(model=model, index=0)
            selector_active = app._needs_periodic_refresh()

            app.state.selector = None
            foreground_turn = app._needs_periodic_refresh()
        finally:
            app._turn_task.cancel()
            try:
                await app._turn_task
            except asyncio.CancelledError:
                pass
            app._turn_task = None
        return selector_active, foreground_turn

    selector_active, foreground_turn = asyncio.run(scenario())
    assert selector_active is False
    assert foreground_turn is True


def test_selector_close_during_foreground_turn_skips_intermediate_running_refresh():
    from nanocode.tui.selector import ChoiceItem, ChoiceModel

    class RecordingApp(RichApp):
        def __init__(self):
            super().__init__(output=_console())
            self.refreshes: list[tuple[str, bool]] = []

        def _refresh(self) -> None:
            self.refreshes.append((self.state.mode, self.state.selector is not None))

    async def scenario():
        app = RecordingApp()
        app._loop = asyncio.get_running_loop()
        app._turn_task = asyncio.create_task(asyncio.sleep(10))
        try:
            model = ChoiceModel("Actions", [ChoiceItem("Open", "open")])
            selector_task = asyncio.create_task(app.run_selector(model))
            await asyncio.sleep(0)
            app._dispatch_selector("escape")
            outcome = await asyncio.wait_for(selector_task, timeout=3)
            return outcome, app.refreshes
        finally:
            app._turn_task.cancel()
            try:
                await app._turn_task
            except asyncio.CancelledError:
                pass

    outcome, refreshes = asyncio.run(scenario())

    assert outcome.kind == "cancel"
    assert ("selector", True) in refreshes
    assert ("running", False) not in refreshes


def test_selector_close_inside_owner_turn_defers_mouse_tracking_until_turn_end():
    from nanocode.tui.selector import ChoiceItem, ChoiceModel

    class RecordingApp(RichApp):
        def __init__(self):
            super().__init__(output=_console())
            self.mouse_tracking_calls: list[bool] = []

        def _mouse_tracking(self, enabled: bool) -> None:
            self.mouse_tracking_calls.append(enabled)

    async def scenario():
        app = RecordingApp()
        app._loop = asyncio.get_running_loop()
        app.bind_thread(FakeThread())

        async def submit(_text: str):
            model = ChoiceModel("Actions", [ChoiceItem("Open", "open")])
            app._loop.call_soon(app._dispatch_selector, "escape")
            await app.run_selector(model)
            assert app._selector_mouse_defer_disable is True
            assert app.mouse_tracking_calls == [True]

        app.set_submit_handler(submit)
        app._submit("open selector")
        await asyncio.wait_for(app._turn_task, timeout=3)
        return app.mouse_tracking_calls, app._selector_mouse_defer_disable

    mouse_calls, deferred = asyncio.run(scenario())

    assert mouse_calls == [True, False]
    assert deferred is False


def test_idle_background_widget_still_accepts_input_submission():
    async def scenario():
        r, w = os.pipe()
        app = RichApp(input=r, output=_console())
        thread = FakeThread(subagent_records=[_running_subagent_record()])
        app.bind_thread(thread)
        calls = []

        async def submit(text: str):
            calls.append(text)

        app.set_submit_handler(submit)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        os.write(w, b"/agents running\r")
        await asyncio.sleep(0.15)
        os.write(w, b"\x04")
        await asyncio.wait_for(task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return calls

    assert asyncio.run(scenario()) == ["/agents running"]


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


def test_completed_message_renders_without_delta():
    async def scenario():
        r, w = os.pipe()
        con = _console()
        app = RichApp(input=r, output=con)
        thread = FakeThread(emit_delta=False, final_text="final-only answer")
        app.bind_thread(thread)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        os.write(w, "hello\r".encode())
        await asyncio.sleep(0.15)
        os.write(w, b"\x04")
        await asyncio.wait_for(task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return con

    con = asyncio.run(scenario())
    assert "final-only answer" in con.file.getvalue()


def test_completed_message_commits_to_scrollback_after_turn_finishes():
    async def scenario():
        r, w = os.pipe()
        con = _console()
        app = RichApp(input=r, output=con)
        final_text = "\n\n".join(f"section {i}: detailed content" for i in range(12))
        thread = FakeThread(emit_delta=True, final_text=final_text)
        app.bind_thread(thread)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(0.05)
        os.write(w, "hello\r".encode())
        await asyncio.sleep(0.15)
        os.write(w, b"\x04")
        await asyncio.wait_for(task, timeout=3)
        os.close(w)
        try:
            os.close(r)
        except OSError:
            pass
        return app, con

    app, con = asyncio.run(scenario())

    scrollback = con.file.getvalue()
    assert "section 0: detailed content" in scrollback
    assert "section 11: detailed content" in scrollback

    out = _render(app, con)
    assert "section 11: detailed content" not in out
    assert all(type(i).__name__ != "AssistantItem" for i in app.state.timeline)


def test_completed_message_stays_live_across_next_user_message():
    from nanocode.tui.state import ToolItem

    con = _console()
    app = RichApp(output=con)
    app.bind_thread(FakeThread())
    app.on_event(_env(E.ToolCallRequested(tool="read_file", input={}, tool_use_id="tu1")))
    app.on_event(_env(E.ToolResultObserved(tool="read_file", tool_use_id="tu1", chars=12, result="file body")))
    app.on_event(_env(E.AssistantMessageCompleted(
        message={}, text="final answer", thinking="", tool_uses=[],
        stop_reason="end", usage=None, latency_ms=1,
    )))

    assert "final answer" not in con.file.getvalue()
    assert any(isinstance(i, ToolItem) for i in app.state.timeline)

    app.on_event(_env(E.UserMessageAccepted(text="next prompt")))

    assert "final answer" not in con.file.getvalue()
    assert "next prompt" not in con.file.getvalue()
    assert any(isinstance(i, ToolItem) for i in app.state.timeline)


def test_completed_message_renders_pi_style_markdown_without_full_width_spam():
    con = _console(width=48)
    app = RichApp(output=con)
    app.bind_thread(FakeThread())
    app.on_event(_env(E.AssistantMessageCompleted(
        message={},
        text=(
            "# Title\n\nSome **bold** and `code`\n\n---\n\n- item\n\n"
            "| A | B |\n|---|---|\n| x | y |\n\n"
            "```python\ndef foo():\n    return 1\n```"
        ),
        thinking="",
        tool_uses=[],
        stop_reason="end",
        usage=None,
        latency_ms=1,
    )))
    con.print(app)

    out = con.file.getvalue()
    import re as _re
    plain = _re.sub(r"\x1b\[[0-9;]*m", "", out)   # 去 SGR:代码经 Syntax 高亮后 token 间夹有色码
    assert "Title" in out
    assert "bold" in out
    assert "code" in out
    assert "• item" in plain          # 列表渲染为排版圆点 • (与源码 '-' 区分),marker 与文本分别着色
    assert "A │ B" in out
    assert "x │ y" in out
    assert "```" not in plain          # fenced code 围栏不外漏(只渲染高亮代码)
    assert "def foo():" in plain
    assert "return 1" in plain
    assert "─" * 80 not in out


def test_streaming_long_assistant_uses_scrollable_transcript_viewport():
    con = _console(height=12, width=80)
    app = RichApp(output=con)
    app.bind_thread(FakeThread())
    app.on_event(_env(E.AssistantDelta(text="\n\n".join(f"section {i}" for i in range(30)))))

    out = _render(app, con)
    assert "repo" in out
    assert "section 29" in out
    assert "section 0" not in out
    assert "earlier transcript lines" not in out

    for _ in range(20):
        app._dispatch_main("pageup")
    out = _render(app, con)
    assert "section 0" in out
    assert "later transcript lines" not in out

    for _ in range(20):
        app._dispatch_main("pagedown")
    out = _render(app, con)
    assert "section 29" in out
    assert "repo" in out


def test_completed_long_assistant_uses_scrollable_transcript_viewport():
    con = _console(height=12, width=80)
    app = RichApp(output=con)
    app.bind_thread(FakeThread())
    app.on_event(_env(E.AssistantMessageCompleted(
        message={},
        text="\n\n".join(f"section {i}" for i in range(30)),
        thinking="",
        tool_uses=[],
        stop_reason="end",
        usage=None,
        latency_ms=1,
    )))

    out = _render(app, con)
    assert "earlier transcript hidden" not in out
    assert "section 29" in out
    assert "section 0" not in out

    for _ in range(20):
        app._dispatch_main("pageup")
    out = _render(app, con)
    assert "section 0" in out
    assert "later transcript lines" not in out

    from nanocode.tui.state import AssistantItem
    stored = [i for i in app.state.timeline if isinstance(i, AssistantItem)][-1]
    assert "section 0" in stored.text
    assert "section 29" in stored.text


def test_bind_thread_does_not_replay_persisted_transcript():
    con = _console()
    app = RichApp(output=con)
    thread = FakeThread()
    thread.transcript_messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
    ]

    app.bind_thread(thread)
    con.print(app)

    out = con.file.getvalue()
    assert "old question" not in out
    assert "old answer" not in out
    assert "repo" in out          # footer cwd proves the bottom input/footer region rendered


def test_long_resumed_transcript_is_not_replayed_into_prompt():
    con = _console(height=12)
    app = RichApp(output=con)
    thread = FakeThread()
    thread.transcript_messages = [
        {"role": "user", "content": f"old question {i}"}
        for i in range(20)
    ]

    app.bind_thread(thread)
    con.print(app)

    out = con.file.getvalue()
    assert "old question" not in out
    assert "repo" in out          # footer cwd proves the bottom input/footer region rendered


def test_refresh_transcript_replays_full_history_to_scrollback():
    con = _console(height=12)
    app = RichApp(output=con)
    thread = FakeThread()
    thread.transcript_messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": [{"type": "text", "text": "\n\n".join(
            f"section {i}: historical answer" for i in range(12)
        )}]},
        {"role": "toolResult", "toolName": "read_file", "content": "tool output"},
    ]

    app.bind_thread(thread)
    app.refresh_transcript()
    con.print(app)

    out = con.file.getvalue()
    assert "Session context" in out
    assert "old question" in out
    assert "section 0: historical answer" in out
    assert "section 11: historical answer" in out
    assert "read_file: tool output" in out
    assert "repo" in out          # footer cwd proves the bottom input/footer region rendered
