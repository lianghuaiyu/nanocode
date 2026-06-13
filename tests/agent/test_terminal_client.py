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
    BudgetExceeded,
    LlmRequestPrepared,
    NoticeRaised,
    RetryRaised,
    SubAgentStarted,
    SubAgentEnded,
    ToolCallAuthorized,
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
    monkeypatch.setattr(ui, "print_info", lambda m: calls.append(("info", m)))
    monkeypatch.setattr(ui, "print_retry", lambda a, m, r: calls.append(("retry", a, m, r)))
    monkeypatch.setattr(ui, "print_sub_agent_start", lambda t, d: calls.append(("sub_start", t, d)))
    monkeypatch.setattr(ui, "print_sub_agent_end", lambda t, d: calls.append(("sub_end", t, d)))
    monkeypatch.setattr(ui, "print_cost", lambda i, o: calls.append(("cost", i, o)))
    monkeypatch.setattr(ui, "print_confirmation", lambda m: calls.append(("confirmation", m)))
    monkeypatch.setattr(ui, "start_spinner", lambda *a: calls.append(("spinner_start",)))
    monkeypatch.setattr(ui, "stop_spinner", lambda: calls.append(("spinner_stop",)))
    return calls


def _env(event) -> dict:
    return {"thread_id": "t", "session_id": "t", "seq": 1, "type": event.kind, "event": event}


# ─── 词表完整性：每个事件 kind 都在 durable 通道表里 ─────────────────────────────

def test_every_event_kind_has_durable_channel_entry():
    kinds = {cls.kind for cls in ALL_AGENT_EVENTS}
    assert kinds == set(DURABLE_ENTRY_FOR_EVENT), "词表与 durable 通道映射必须一一对应"


# ─── TerminalClient.on_event：事件 → 渲染映射 ────────────────────────────────

def _content(calls):
    """滤掉 spinner 起停噪声，只看内容渲染调用。"""
    return [c for c in calls if c[0] not in ("spinner_start", "spinner_stop")]


def test_client_renders_assistant_delta_text_and_thinking(monkeypatch):
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(AssistantDelta(text="hello ")))
    c.on_event(_env(AssistantDelta(thinking="reasoning")))
    assert _content(calls) == [("assistant_markdown", "hello "), ("thinking", "reasoning")]


def test_client_renders_tool_call_and_result(monkeypatch):
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(ToolCallRequested(tool="run_shell", input={"command": "ls"}, tool_use_id="t1")))
    c.on_event(_env(ToolResultObserved(tool="read_file", tool_use_id="x", chars=3, result="abc")))
    assert _content(calls) == [("tool_call", "run_shell", {"command": "ls"}),
                               ("tool_result", "read_file", "abc")]


def test_client_renders_phase2_events(monkeypatch):
    # docs/17 Phase 2：notice/retry/sub_agent/budget/deny/cost 从 sink 直渲迁到事件流渲染。
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(NoticeRaised(text="heads up")))
    c.on_event(_env(RetryRaised(attempt=1, max_retries=3, reason="timeout")))
    c.on_event(_env(SubAgentStarted(agent_type="explore", description="scan")))
    c.on_event(_env(SubAgentEnded(agent_type="explore", description="scan")))
    c.on_event(_env(BudgetExceeded(reason="cost cap")))
    c.on_event(_env(ToolCallAuthorized(tool="run_shell", action="deny", message="blocked")))
    c.on_event(_env(TurnCompleted(input_tokens=10, output_tokens=20, turns=1, cost_usd=0.01)))
    assert _content(calls) == [
        ("info", "heads up"),
        ("retry", 1, 3, "timeout"),
        ("sub_start", "explore", "scan"),
        ("sub_end", "explore", "scan"),
        ("info", "Budget exceeded: cost cap"),
        ("info", "Denied: blocked"),
        ("cost", 10, 20),
    ]


def test_client_tool_call_authorized_allow_is_noop(monkeypatch):
    calls = _capture_ui(monkeypatch)
    TerminalClient().on_event(_env(ToolCallAuthorized(tool="read_file", action="allow")))
    assert _content(calls) == []


def test_client_renders_approval_requested(monkeypatch):
    # docs/17 Phase 4：审批显示经事件流（client 渲染告警）；y/n 决策经注入的 confirm_fn。
    from nanocode.agent.events import ApprovalRequested
    calls = _capture_ui(monkeypatch)
    TerminalClient().on_event(_env(ApprovalRequested(command="rm -rf /", message="⚠ rm -rf /", request_id="abc")))
    assert _content(calls) == [("confirmation", "⚠ rm -rf /")]


def test_client_derives_spinner(monkeypatch):
    """spinner client 派生：llm_request_prepared 起，首个内容事件停（旧 cfg.sink.spinner_*）。"""
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(LlmRequestPrepared(model="m", message_count=1, messages_chars=2)))
    c.on_event(_env(AssistantDelta(text="hi")))
    spinner = [k[0] for k in calls if k[0].startswith("spinner")]
    assert spinner == ["spinner_start", "spinner_stop"]
    # spinner_stop 必须在渲染 markdown 之前
    assert calls.index(("spinner_stop",)) < calls.index(("assistant_markdown", "hi"))


def test_client_retry_does_not_stop_spinner(monkeypatch):
    # retry 发生在同一 stream_fn 调用内，无后续 llm_request_prepared 重启 spinner → 不停 spinner。
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(LlmRequestPrepared(model="m", message_count=1, messages_chars=2)))
    c.on_event(_env(RetryRaised(attempt=1, max_retries=3, reason="boom")))
    assert ("spinner_stop",) not in calls
    assert ("retry", 1, 3, "boom") in calls


def test_client_durable_only_events_are_ui_noop(monkeypatch):
    calls = _capture_ui(monkeypatch)
    c = TerminalClient()
    c.on_event(_env(AssistantMessageCompleted(
        message={"role": "assistant"}, text="hi", thinking="", tool_uses=[],
        stop_reason="stop", usage=None, latency_ms=None)))   # 整段不重复渲染（流式已逐 block）
    assert _content(calls) == []


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
    assert _content(calls) == [("tool_call", "run_shell", {"command": "ls"})]


def test_agent_emit_assistant_delta_renders_text_and_thinking(monkeypatch):
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt3", permission_mode="bypassPermissions")
    calls = _subscribed_client(a, monkeypatch)
    a.emit(AssistantDelta(text="hello "))
    a.emit(AssistantDelta(thinking="hmm"))
    assert _content(calls) == [("assistant_markdown", "hello "), ("thinking", "hmm")]
