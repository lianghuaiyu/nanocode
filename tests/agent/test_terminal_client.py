"""TerminalClient 渲染 + Agent.emit→订阅扇出 + 事件词表完整性（docs/17 Phase 1）。

docs/17：UI 投影腿（project_agent_event→EventSink）已删——assistant/tool 的流式渲染改由订阅端
TerminalClient 从事件流渲染。本测试锚定 client 的事件→ui 映射、durable-only 事件不渲染、以及
`Agent.emit` 经 `_event_subscribers` 把事件推给订阅者的 live 接线。
"""

import nanocode.ui as ui
from nanocode.entrypoints.terminal_client import TerminalClient
from nanocode.agent.events import (
    ALL_AGENT_EVENTS,
    DURABLE_ENTRY_FOR_EVENT,
    AssistantDelta,
    AssistantMessageCompleted,
    LlmRequestPrepared,
    ToolCallRequested,
    ToolResultObserved,
    TurnCompleted,
)


def _capture_ui(monkeypatch):
    """把 ui 渲染原语替成记录器，返回 calls 列表。"""
    calls = []
    monkeypatch.setattr(ui, "render_assistant_markdown", lambda t: calls.append(("assistant_markdown", t)))
    monkeypatch.setattr(ui, "render_thinking", lambda t: calls.append(("thinking", t)))
    monkeypatch.setattr(ui, "print_tool_call", lambda n, i: calls.append(("tool_call", n, i)))
    monkeypatch.setattr(ui, "print_tool_result", lambda n, r: calls.append(("tool_result", n, r)))
    return calls


def _env(event) -> dict:
    return {"thread_id": "t", "session_id": "t", "seq": 1, "type": event.kind, "event": event}


# ─── 词表完整性：每个事件 kind 都在 durable 通道表里 ─────────────────────────────

def test_every_event_kind_has_durable_channel_entry():
    kinds = {cls.kind for cls in ALL_AGENT_EVENTS}
    assert kinds == set(DURABLE_ENTRY_FOR_EVENT), "词表与 durable 通道映射必须一一对应"


# ─── TerminalClient.on_event：流式渲染（仅三种事件有 UI）────────────────────────

def test_client_renders_assistant_delta_text_and_thinking(monkeypatch):
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(AssistantDelta(text="hello ")))
    c.on_event(_env(AssistantDelta(thinking="reasoning")))
    assert calls == [("assistant_markdown", "hello "), ("thinking", "reasoning")]


def test_client_renders_tool_call_and_result(monkeypatch):
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(ToolCallRequested(tool="run_shell", input={"command": "ls"}, tool_use_id="t1")))
    c.on_event(_env(ToolResultObserved(tool="read_file", tool_use_id="x", chars=3, result="abc")))
    assert calls == [("tool_call", "run_shell", {"command": "ls"}),
                     ("tool_result", "read_file", "abc")]


def test_client_durable_only_events_are_ui_noop(monkeypatch):
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(LlmRequestPrepared(model="m", message_count=1, messages_chars=2)))
    c.on_event(_env(TurnCompleted(input_tokens=1, output_tokens=2, turns=1)))
    c.on_event(_env(AssistantMessageCompleted(
        message={"role": "assistant"}, text="hi", thinking="", tool_uses=[],
        stop_reason="stop", usage=None, latency_ms=None)))   # 整段不重复渲染（流式已逐 block）
    assert calls == []


# ─── Agent.emit live 接线（docs/17：事件经 _event_subscribers 推给订阅的 client）─────

def _subscribed_client(agent, monkeypatch):
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    # 复刻 RuntimeThread 的 tap：把 emit 的 typed 事件包成信封交给 client。
    agent._event_subscribers.append(lambda ev: c.on_event(_env(ev)))
    return calls


def test_agent_emit_tool_call_requested_renders_via_client(monkeypatch):
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt1", permission_mode="bypassPermissions")
    calls = _subscribed_client(a, monkeypatch)
    a.emit(ToolCallRequested(tool="run_shell", input={"command": "ls"}, tool_use_id="tu1"))
    assert calls == [("tool_call", "run_shell", {"command": "ls"})]


def test_agent_emit_assistant_delta_renders_text_and_thinking(monkeypatch):
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt3", permission_mode="bypassPermissions")
    calls = _subscribed_client(a, monkeypatch)
    a.emit(AssistantDelta(text="hello "))
    a.emit(AssistantDelta(thinking="hmm"))
    assert calls == [("assistant_markdown", "hello "), ("thinking", "hmm")]
