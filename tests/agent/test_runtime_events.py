"""typed AgentEvent UI 投影（project_agent_event）+ Agent.emit 单出口扇出的薄层单元测试。

docs/16 #4：string-channel（RuntimeEvent/DURABLE_TYPES/EPHEMERAL_UI_TYPES/EventDispatcher）
已整体删除——live 路径全 typed。durable 通道映射见 events.DURABLE_ENTRY_FOR_EVENT。
"""

from nanocode.agent.runtime_events import project_agent_event
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


class _RecSink:
    """记录 12 个 EventSink 方法的调用。"""
    def __init__(self): self.calls = []
    def assistant_markdown(self, text): self.calls.append(("assistant_markdown", text))
    def thinking(self, text): self.calls.append(("thinking", text))
    def spinner_start(self, label="Thinking"): self.calls.append(("spinner_start", label))
    def spinner_stop(self): self.calls.append(("spinner_stop",))
    def tool_call(self, name, inp): self.calls.append(("tool_call", name, inp))
    def tool_result(self, name, result): self.calls.append(("tool_result", name, result))
    def cost(self, i, o): self.calls.append(("cost", i, o))
    def info(self, message): self.calls.append(("info", message))
    def confirmation(self, command): self.calls.append(("confirmation", command))
    def sub_agent_start(self, t, d): self.calls.append(("sub_agent_start", t, d))
    def sub_agent_end(self, t, d): self.calls.append(("sub_agent_end", t, d))
    def retry(self, a, m, r): self.calls.append(("retry", a, m, r))


# ─── 词表完整性：每个事件 kind 都在 durable 通道表里 ─────────────────────────────

def test_every_event_kind_has_durable_channel_entry():
    kinds = {cls.kind for cls in ALL_AGENT_EVENTS}
    assert kinds == set(DURABLE_ENTRY_FOR_EVENT), "词表与 durable 通道映射必须一一对应"


# ─── project_agent_event：UI 投影（仅三种事件有 UI）────────────────────────────

def test_project_assistant_delta_text_and_thinking():
    sk = _RecSink()
    project_agent_event(AssistantDelta(text="hello "), sk)
    project_agent_event(AssistantDelta(thinking="reasoning"), sk)
    assert sk.calls == [("assistant_markdown", "hello "), ("thinking", "reasoning")]


def test_project_tool_call_and_result():
    sk = _RecSink()
    project_agent_event(ToolCallRequested(tool="run_shell", input={"command": "ls"}, tool_use_id="t1"), sk)
    project_agent_event(ToolResultObserved(tool="read_file", tool_use_id="x", chars=3, result="abc"), sk)
    assert sk.calls == [("tool_call", "run_shell", {"command": "ls"}),
                        ("tool_result", "read_file", "abc")]


def test_project_durable_only_events_are_ui_noop():
    sk = _RecSink()
    project_agent_event(LlmRequestPrepared(model="m", message_count=1, messages_chars=2), sk)
    project_agent_event(TurnCompleted(input_tokens=1, output_tokens=2, turns=1), sk)
    project_agent_event(AssistantMessageCompleted(
        message={"role": "assistant"}, text="hi", thinking="", tool_uses=[],
        stop_reason="stop", usage=None, latency_ms=None), sk)   # 整段不重复渲染（流式已逐 block）
    assert sk.calls == []


# ─── Agent.emit live 接线（docs/16 #2：typed AgentEvent 单出口，UI 腿投影到 sink）─────

def test_agent_emit_tool_call_requested_projects():
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt1", permission_mode="bypassPermissions")
    sk = _RecSink()
    a._sink = sk
    a.emit(ToolCallRequested(tool="run_shell", input={"command": "ls"}, tool_use_id="tu1"))
    assert sk.calls == [("tool_call", "run_shell", {"command": "ls"})]


def test_agent_emit_tool_result_observed_projects():
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt2", permission_mode="bypassPermissions")
    sk = _RecSink()
    a._sink = sk
    a.emit(ToolResultObserved(tool="read_file", tool_use_id="x", chars=3, result="abc"))
    assert sk.calls == [("tool_result", "read_file", "abc")]


def test_agent_emit_assistant_delta_projects_text_and_thinking():
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt3", permission_mode="bypassPermissions")
    sk = _RecSink()
    a._sink = sk
    a.emit(AssistantDelta(text="hello "))
    a.emit(AssistantDelta(thinking="hmm"))
    assert sk.calls == [("assistant_markdown", "hello "), ("thinking", "hmm")]
