"""RUNTIME-P1 Step 1：RuntimeEvent 流 / EventDispatcher / projection 的薄层单元 + parity 测试。

本层尚未接入 live path（backends 仍双发），故这里是单元级 parity：
- 钉住静态分类表（golden 集 + 不相交）——任何改动都须是有意识的；
- 钉住 dispatcher 路由：durable → Tracer.emit；always → UI projection；ephemeral 不写 tracer；
- 钉住每个 type 的 projection → 正确的 EventSink 方法/参数。
（接入 live path 后的「wire byte-parity」turn 级断言属 Step 2。）
"""

from nanocode.agent import runtime_events as re
from nanocode.agent.runtime_events import RuntimeEvent, EventDispatcher, project


# ─── 录制替身 ─────────────────────────────────────────────────

class _RecTracer:
    def __init__(self): self.emits = []
    def emit(self, type, **fields): self.emits.append((type, fields))


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


# ─── 静态分类表（golden）────────────────────────────────────────

def test_durable_types_golden_set():
    # 必须精确等于今天经 tracer.emit 落 wire 的 13 个 type（盘点 workflow 核实）。
    assert re.DURABLE_TYPES == frozenset({
        "session_start", "user_message", "llm_request", "assistant_message",
        "llm_response", "budget_exceeded", "tool_call", "tool_result",
        "permission_decision", "tool_blocked", "compaction", "turn_end", "session_end",
    })
    # tool_blocked 必须在内（它无 summarizer，但仍 durable —— 防「只持久可渲染 type」式误过滤）。
    assert "tool_blocked" in re.DURABLE_TYPES


def test_ephemeral_ui_types_golden_set():
    assert re.EPHEMERAL_UI_TYPES == frozenset({
        "assistant_block", "assistant_thinking",
        "spinner_start", "spinner_stop", "cost", "info",
        "confirmation", "sub_agent_start", "sub_agent_end", "retry",
    })
    # sub_agent_start/end 是 ephemeral（今天无 tracer.emit；durable 记录是子 agent 自身的
    # session_start/end 在子 wire）—— 提升为 durable 会给父 wire 加新行，破 byte-parity。
    assert "sub_agent_start" in re.EPHEMERAL_UI_TYPES
    assert "sub_agent_end" in re.EPHEMERAL_UI_TYPES
    # assistant_markdown/thinking 不是独立 type（是 assistant_message 的投影）。
    assert "assistant_markdown" not in re.EPHEMERAL_UI_TYPES
    assert "thinking" not in re.EPHEMERAL_UI_TYPES


def test_durable_and_ephemeral_disjoint():
    assert re.DURABLE_TYPES.isdisjoint(re.EPHEMERAL_UI_TYPES)


def test_is_durable():
    assert re.is_durable("tool_call") is True
    assert re.is_durable("spinner_start") is False
    assert re.is_durable("nonsense") is False


# ─── dispatcher 路由 ──────────────────────────────────────────

def _dispatch(event):
    tr, sk = _RecTracer(), _RecSink()
    EventDispatcher(tr, sk).dispatch(event)
    return tr, sk


def test_dispatch_durable_with_ui_emits_and_projects():
    # tool_call：真·同时刻双发 —— durable tracer.emit + UI sink.tool_call。
    tr, sk = _dispatch(RuntimeEvent("tool_call", {"tool": "run_shell", "input": {"command": "ls"}, "tool_use_id": "tu1"}))
    assert tr.emits == [("tool_call", {"tool": "run_shell", "input": {"command": "ls"}, "tool_use_id": "tu1"})]
    assert sk.calls == [("tool_call", "run_shell", {"command": "ls"})]  # tool->name, input->inp


def test_dispatch_durable_only_no_ui():
    tr, sk = _dispatch(RuntimeEvent("session_start", {"model": "m", "is_sub_agent": False}))
    assert tr.emits == [("session_start", {"model": "m", "is_sub_agent": False})]
    assert sk.calls == []  # 无 UI 投影


def test_dispatch_ephemeral_no_tracer():
    tr, sk = _dispatch(RuntimeEvent("spinner_start", {}))
    assert tr.emits == []           # ephemeral 绝不写 wire
    assert sk.calls == [("spinner_start", "Thinking")]


def test_dispatch_assistant_message_is_durable_no_ui():
    # assistant_message：durable（写 wire），无 UI 投影 —— 流式已由 assistant_block/thinking 渲染。
    tr, sk = _dispatch(RuntimeEvent("assistant_message", {"text": "hi", "thinking": "hmm", "tool_uses": []}))
    assert tr.emits == [("assistant_message", {"text": "hi", "thinking": "hmm", "tool_uses": []})]
    assert sk.calls == []


def test_dispatch_assistant_block_and_thinking_are_ui_only():
    tr, sk = _dispatch(RuntimeEvent("assistant_block", {"text": "hello "}))
    assert tr.emits == []                          # ephemeral，不写 wire
    assert sk.calls == [("assistant_markdown", "hello ")]
    tr2, sk2 = _dispatch(RuntimeEvent("assistant_thinking", {"text": "reasoning"}))
    assert tr2.emits == []
    assert sk2.calls == [("thinking", "reasoning")]


def test_dispatch_tool_result_maps_fields():
    tr, sk = _dispatch(RuntimeEvent("tool_result", {"tool": "read_file", "tool_use_id": "x", "chars": 3, "result": "abc"}))
    assert tr.emits[0][0] == "tool_result"
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
    assert sk.calls == []  # 这些 durable type 无 UI 投影


# ─── Step 2a：Agent._dispatch_event live 接线（tool I/O 类已迁移）────────────────

def test_agent_dispatch_event_tool_call_emits_and_projects():
    """真 Agent 上 _dispatch_event('tool_call') 同时：durable→tracer.emit + UI→sink.tool_call。
    钉住 backends 已从双发切到单流且行为逐字等价。"""
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", trace_enabled=False, session_id="rtevt1")
    tr, sk = _RecTracer(), _RecSink()
    a.tracer, a._sink = tr, sk   # _dispatch_event 读 live self.tracer/self._sink
    a._dispatch_event("tool_call", tool="run_shell", input={"command": "ls"}, tool_use_id="tu1")
    assert tr.emits == [("tool_call", {"tool": "run_shell", "input": {"command": "ls"}, "tool_use_id": "tu1"})]
    assert sk.calls == [("tool_call", "run_shell", {"command": "ls"})]


def test_agent_dispatch_event_tool_result_emits_and_projects():
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", trace_enabled=False, session_id="rtevt2")
    tr, sk = _RecTracer(), _RecSink()
    a.tracer, a._sink = tr, sk
    a._dispatch_event("tool_result", tool="read_file", tool_use_id="x", chars=3, result="abc")
    assert tr.emits == [("tool_result", {"tool": "read_file", "tool_use_id": "x", "chars": 3, "result": "abc"})]
    assert sk.calls == [("tool_result", "read_file", "abc")]
