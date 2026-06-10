"""单一 RuntimeEvent 流 + dispatcher + sinks/projection（RUNTIME-P1 Step 1，Pi-aligned）。

目标模型（Pi 证明 core event stream 才是核心，EventBus/dispatcher 只做 fanout，
session/wire/UI 互不调用）：

    Agent core ── emit RuntimeEvent ──►
        ├─ durable sink   ：经 Tracer.emit 写 wire.jsonl（过渡期复用现有 Tracer，不另造 wire 逻辑）
        ├─ EventSinkProjection：渲染终端 UI（EventSink 的 12 个方法）
        ├─（后续）Buffer 订阅：TurnResult.final_response capture
        ├─（后续）RuntimeThread.events()：in-process subscription
        └─（后续）JSON-RPC notification：protocol projection

逐类把双发改成一次 dispatch(RuntimeEvent)，由 durable sink 与 projection 分别处理。
进度（RUNTIME-P1）：tool_call / tool_result（tool I/O 类）已迁移（Step 2a）；其余事件仍双发，逐类推进。

byte-parity 第一铁律：分类（durable / ephemeral）是**静态、按 type 字符串的表**，绝不作为事件
payload 上的 kwarg —— 否则 ui=/ui_only= 会落进 wire 顶层并折进 SessionEvent.data 污染 wire。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# 今天经 tracer.emit 落 wire.jsonl 的 13 个 type。DURABLE 集必须**精确等于**此集：
#   - 增（如 sub_agent_start/end、spinner、retry、逐 block 的 assistant 文本）→ wire 多出新行 → 破 parity；
#   - 删（如因 tool_blocked 无 summarizer 而过滤掉）→ wire 丢行 → 破 parity。
DURABLE_TYPES = frozenset({
    "session_start", "user_message", "llm_request", "assistant_message",
    "llm_response", "budget_exceeded", "tool_call", "tool_result",
    "permission_decision", "tool_blocked", "compaction", "turn_end", "session_end",
})

# durable wire schema 契约（RUNTIME-P1 ↔ Trajectory 桥）：每个 durable type 在 wire 上的稳定
# payload 字段。trajectory.project/metrics/eval 与 resume 重建都依赖它们；改任一 type 或字段名
# = 破契约（trajectory 会静默崩）。约束：只能 flat-additive 增字段，不改名/不删（docs/10 §兼容性、
# docs/12 边界 5）。SUMMARY 级整形见 trace.redaction：llm_request.messages → messages_hash/
# messages_chars，tool_result.result → result_summary/result_hash（投影层 project 有兜底）。
# guard: tests/agent/test_durable_schema_contract.py。
DURABLE_EVENT_FIELDS: "dict[str, frozenset[str]]" = {
    "session_start": frozenset({"model", "cwd", "permission_mode", "is_sub_agent", "workspace_trusted"}),
    "user_message": frozenset({"text"}),
    "llm_request": frozenset({"model", "message_count", "messages"}),
    "assistant_message": frozenset({"text", "thinking", "tool_uses"}),
    "llm_response": frozenset({"input_tokens", "output_tokens"}),
    "budget_exceeded": frozenset({"reason"}),
    "tool_call": frozenset({"tool", "input", "tool_use_id"}),
    "tool_result": frozenset({"tool", "tool_use_id", "chars", "result"}),
    "permission_decision": frozenset({"tool", "action", "message"}),
    "tool_blocked": frozenset({"tool", "reason", "agent_type", "artifact_id"}),
    "compaction": frozenset({"kind", "message_count_before", "message_count_after"}),
    "turn_end": frozenset({"input_tokens", "output_tokens", "turns"}),
    "session_end": frozenset({"input_tokens", "output_tokens", "turns"}),
}

# 仅 UI、不落 wire 的 RuntimeEvent type。流式文本/思考的 UI 经 ephemeral assistant_block /
# assistant_thinking 逐 block 渲染（durable 记录是整段 assistant_message，本身**不**做 UI 投影，
# 既避免重复渲染、又保留 Anthropic 的逐 block 流式）。tool_call / tool_result 是 durable type
# （同名 sink 方法是其 UI 投影）。
EPHEMERAL_UI_TYPES = frozenset({
    "assistant_block", "assistant_thinking",
    "spinner_start", "spinner_stop", "cost", "info",
    "confirmation", "sub_agent_start", "sub_agent_end", "retry",
})


def is_durable(event_type: str) -> bool:
    """该 type 是否应被持久化到 wire.jsonl。"""
    return event_type in DURABLE_TYPES


@dataclass(frozen=True)
class RuntimeEvent:
    """core 发出的一条事件：type + 扁平 fields（与今天 tracer.emit 的 payload kwargs 同形）。

    分类信息**不**在此（不带 ui= 标志）—— 由 DURABLE_TYPES/EPHEMERAL_UI_TYPES 静态表判定。
    """
    type: str
    fields: dict = field(default_factory=dict)


def project(event: RuntimeEvent, sink) -> None:
    """EventSinkProjection：把一条 RuntimeEvent 渲染成 EventSink 调用（UI 投影）。

    durable tool_call/tool_result → 同名 sink；assistant_message 是 durable 但**无 UI 投影**
    （流式文本/思考已由 ephemeral assistant_block / assistant_thinking 逐 block 渲染，避免重复、
    保留 Anthropic 逐 block 流式）；其余无 UI 的 durable 事件（session_*/user_message/llm_*/
    turn_end/budget_exceeded/permission_decision/tool_blocked/compaction）在此 no-op。

    注：cost 事件携带累计 totals（与今天 _sink.cost 传 total_* 一致），故投影无状态；
    confirmation 的「阻塞读用户输入」不可由被动投影复现——迁移时阻塞 confirm 仍走 live 路径，
    本投影只渲染显示回显（见 RUNTIME-P1 Step 2 备注）。
    """
    t, f = event.type, event.fields
    if t == "assistant_block":
        sink.assistant_markdown(f.get("text", ""))
    elif t == "assistant_thinking":
        sink.thinking(f.get("text", ""))
    elif t == "tool_call":
        sink.tool_call(f.get("tool"), f.get("input"))
    elif t == "tool_result":
        sink.tool_result(f.get("tool"), f.get("result"))
    elif t == "spinner_start":
        sink.spinner_start(f.get("label", "Thinking"))
    elif t == "spinner_stop":
        sink.spinner_stop()
    elif t == "cost":
        sink.cost(f.get("input_tokens", 0), f.get("output_tokens", 0))
    elif t == "info":
        sink.info(f.get("message", ""))
    elif t == "confirmation":
        sink.confirmation(f.get("command", ""))
    elif t == "sub_agent_start":
        sink.sub_agent_start(f.get("agent_type", ""), f.get("description", ""))
    elif t == "sub_agent_end":
        sink.sub_agent_end(f.get("agent_type", ""), f.get("description", ""))
    elif t == "retry":
        sink.retry(f.get("attempt", 0), f.get("max_retries", 0), f.get("reason", ""))
    # 其余 durable type 无 UI 投影 → no-op。


def dispatch_event(event: RuntimeEvent, tracer, sink) -> None:
    """fan-out 一条 RuntimeEvent：durable → tracer.emit（写 wire），always → UI projection。

    tracer/sink 由调用方在**调用时**提供（Agent 传 live self.tracer / self._sink，避免缓存
    过期——adopt() 可能把 _sink 包成 TeeSink）。Agent._dispatch_event 与 EventDispatcher 共用本函数。
    """
    if is_durable(event.type):
        tracer.emit(event.type, **event.fields)
    project(event, sink)


class EventDispatcher:
    """单一 RuntimeEvent 流的 fan-out：durable → Tracer（写 wire），always → UI projection。

    过渡期 durable sink **复用现有 Tracer.emit**（byte-identical，不另造 wire 逻辑）；UI 经
    project() 调 EventSink。后续可再挂 Buffer capture / RuntimeThread.events() / 协议 notification
    等订阅者——它们都只是同一条流的投影，互不调用。

    Step 1 不接 live path（backends 仍直接双发）；本类先存在 + 被 parity 测试 exercise。
    """

    def __init__(self, tracer, sink) -> None:
        self._tracer = tracer   # durable sink（现有 Tracer；emit(type, **fields) 写 wire）
        self._sink = sink       # UI EventSink

    def dispatch(self, event: RuntimeEvent) -> None:
        dispatch_event(event, self._tracer, self._sink)
