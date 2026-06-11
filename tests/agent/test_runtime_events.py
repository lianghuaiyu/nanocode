"""RuntimeEvent 流 / EventDispatcher / projection 的薄层单元测试。

docs/14 Milestone B：Tracer/wire 已退役——`dispatch_event(event, sink)` 只做 UI projection；
durable 持久化已迁至 canonical 树（Agent._tree_record / _tree_event）。DURABLE_TYPES /
EPHEMERAL_UI_TYPES 仍作为 RuntimeEvent 的**静态分类表**保留（is_durable 仍可查），但 dispatcher
不再据此写任何 tracer——durable type 只是「无 UI 投影」。
"""

from nanocode.agent import runtime_events as re
from nanocode.agent.runtime_events import RuntimeEvent, EventDispatcher, project


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


# ─── 静态分类表（golden，仍保留作 RuntimeEvent 分类）─────────────────────────────

def test_durable_types_golden_set():
    assert re.DURABLE_TYPES == frozenset({
        "session_start", "user_message", "llm_request", "assistant_message",
        "llm_response", "budget_exceeded", "tool_call", "tool_result",
        "permission_decision", "tool_blocked", "compaction", "turn_end", "session_end",
    })


def test_ephemeral_ui_types_golden_set():
    assert re.EPHEMERAL_UI_TYPES == frozenset({
        "assistant_block", "assistant_thinking",
        "spinner_start", "spinner_stop", "cost", "info",
        "confirmation", "sub_agent_start", "sub_agent_end", "retry",
    })
    assert "assistant_markdown" not in re.EPHEMERAL_UI_TYPES
    assert "thinking" not in re.EPHEMERAL_UI_TYPES


def test_durable_and_ephemeral_disjoint():
    assert re.DURABLE_TYPES.isdisjoint(re.EPHEMERAL_UI_TYPES)


def test_is_durable():
    assert re.is_durable("tool_call") is True
    assert re.is_durable("spinner_start") is False
    assert re.is_durable("nonsense") is False


# ─── dispatcher 路由（仅 UI projection；无 tracer）────────────────────────────────

def _dispatch(event):
    sk = _RecSink()
    EventDispatcher(sk).dispatch(event)
    return sk


def test_dispatch_durable_with_ui_projects():
    # tool_call：durable 但有 UI 投影 → sink.tool_call（durable 持久化由树承担，不在本流）。
    sk = _dispatch(RuntimeEvent("tool_call", {"tool": "run_shell", "input": {"command": "ls"}, "tool_use_id": "tu1"}))
    assert sk.calls == [("tool_call", "run_shell", {"command": "ls"})]


def test_dispatch_durable_only_no_ui():
    sk = _dispatch(RuntimeEvent("session_start", {"model": "m", "is_sub_agent": False}))
    assert sk.calls == []  # durable-only：无 UI 投影


def test_dispatch_ephemeral_projects():
    sk = _dispatch(RuntimeEvent("spinner_start", {}))
    assert sk.calls == [("spinner_start", "Thinking")]


def test_dispatch_assistant_message_no_ui():
    # assistant_message durable（落树），无 UI 投影——流式已由 assistant_block/thinking 渲染。
    sk = _dispatch(RuntimeEvent("assistant_message", {"text": "hi", "thinking": "hmm", "tool_uses": []}))
    assert sk.calls == []


def test_dispatch_assistant_block_and_thinking_are_ui():
    sk = _dispatch(RuntimeEvent("assistant_block", {"text": "hello "}))
    assert sk.calls == [("assistant_markdown", "hello ")]
    sk2 = _dispatch(RuntimeEvent("assistant_thinking", {"text": "reasoning"}))
    assert sk2.calls == [("thinking", "reasoning")]


def test_dispatch_tool_result_maps_fields():
    sk = _dispatch(RuntimeEvent("tool_result", {"tool": "read_file", "tool_use_id": "x", "chars": 3, "result": "abc"}))
    assert sk.calls == [("tool_result", "read_file", "abc")]


# ─── projection 逐 type 映射 ─────────────────────────────────────

def test_project_ephemeral_mappings():
    sk = _RecSink()
    project(RuntimeEvent("cost", {"input_tokens": 10, "output_tokens": 4}), sk)
    project(RuntimeEvent("info", {"message": "note"}), sk)
    project(RuntimeEvent("confirmation", {"command": "rm -rf"}), sk)
    project(RuntimeEvent("sub_agent_start", {"agent_type": "coder", "description": "d"}), sk)
    project(RuntimeEvent("sub_agent_end", {"agent_type": "coder", "description": "d"}), sk)
    project(RuntimeEvent("retry", {"attempt": 1, "max_retries": 3, "reason": "429"}), sk)
    project(RuntimeEvent("spinner_stop", {}), sk)
    assert sk.calls == [
        ("cost", 10, 4),
        ("info", "note"),
        ("confirmation", "rm -rf"),
        ("sub_agent_start", "coder", "d"),
        ("sub_agent_end", "coder", "d"),
        ("retry", 1, 3, "429"),
        ("spinner_stop",),
    ]


def test_project_durable_only_types_are_noop_for_ui():
    sk = _RecSink()
    for t in ("session_start", "user_message", "llm_request", "assistant_message",
              "llm_response", "budget_exceeded", "permission_decision", "tool_blocked",
              "compaction", "turn_end", "session_end"):
        project(RuntimeEvent(t, {"x": 1}), sk)
    assert sk.calls == []  # durable-only type 无 UI 投影


# ─── Agent._dispatch_event live 接线（只投影到 sink；无 tracer）────────────────

def test_agent_dispatch_event_tool_call_projects():
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt1", permission_mode="bypassPermissions")
    sk = _RecSink()
    a._sink = sk
    a._dispatch_event("tool_call", tool="run_shell", input={"command": "ls"}, tool_use_id="tu1")
    assert sk.calls == [("tool_call", "run_shell", {"command": "ls"})]


def test_agent_dispatch_event_tool_result_projects():
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", session_id="rtevt2", permission_mode="bypassPermissions")
    sk = _RecSink()
    a._sink = sk
    a._dispatch_event("tool_result", tool="read_file", tool_use_id="x", chars=3, result="abc")
    assert sk.calls == [("tool_result", "read_file", "abc")]
