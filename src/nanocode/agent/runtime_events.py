"""单一 RuntimeEvent 流 + dispatcher + sinks/projection（Pi-aligned）。

目标模型（Pi 证明 core event stream 才是核心，dispatcher 只做 fanout，
session/UI 互不调用）：

    Agent core ── emit RuntimeEvent ──►
        ├─ EventSinkProjection：渲染终端 UI（EventSink 的 12 个方法）
        ├─（后续）Buffer 订阅：TurnResult.final_response capture
        ├─（后续）RuntimeThread.events()：in-process subscription
        └─（后续）JSON-RPC notification：protocol projection

durable 持久化已不在本流：Milestone B 把派生遥测落进 canonical session.jsonl 树（Agent._tree_event
/_tree_record），wire/Tracer 已退役。本流只做 UI 投影（project → EventSink）。

第一铁律：分类（durable / ephemeral）仍是**静态、按 type 字符串的表**，绝不作为事件 payload 上的
kwarg —— 历史上用于 wire byte-parity，现仍用于区分哪些 type 有 UI 投影。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# 历史上经 tracer.emit 落 wire 的 13 个 type（wire 已退役）。保留作 RuntimeEvent 的**静态分类**：
# 这些 type 的持久化等价物现写进 canonical 树（Agent._tree_event/_tree_record），故 project()
# 对它们多为 no-op（除 tool_call/tool_result 有 UI 投影）。
DURABLE_TYPES = frozenset({
    "session_start", "user_message", "llm_request", "assistant_message",
    "llm_response", "budget_exceeded", "tool_call", "tool_result",
    "permission_decision", "tool_blocked", "compaction", "turn_end", "session_end",
})

# 历史 durable schema 契约：每个 durable type 在事件流上的稳定 payload 字段。沿用作 RuntimeEvent
# payload 形状参考（trajectory 现从 canonical 树派生，B2）。约束：只能 flat-additive 增字段，
# 不改名/不删。
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

# 仅 UI 的 RuntimeEvent type（无树持久化等价物）。流式文本/思考的 UI 经 ephemeral assistant_block /
# assistant_thinking 逐 block 渲染（持久记录是整段 assistant_message，本身**不**做 UI 投影，
# 既避免重复渲染、又保留 Anthropic 的逐 block 流式）。tool_call / tool_result 是 durable type
# （同名 sink 方法是其 UI 投影）。
EPHEMERAL_UI_TYPES = frozenset({
    "assistant_block", "assistant_thinking",
    "spinner_start", "spinner_stop", "cost", "info",
    "confirmation", "sub_agent_start", "sub_agent_end", "retry",
})


def is_durable(event_type: str) -> bool:
    """该 type 是否属 durable 分类（持久化等价物落 canonical 树；wire 已退役）。"""
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


def dispatch_event(event: RuntimeEvent, sink) -> None:
    """fan-out 一条 RuntimeEvent：UI projection（durable 持久化已迁出本流——见 Agent._tree_event/
    _tree_record 落 canonical 树；wire/Tracer 已退役）。

    sink 由调用方在**调用时**提供（Agent 传 live self._sink，避免缓存过期——adopt() 可能把 _sink
    包成 TeeSink）。Agent._dispatch_event 与 EventDispatcher 共用本函数。
    """
    project(event, sink)


class EventDispatcher:
    """单一 RuntimeEvent 流的 fan-out：UI projection（经 project() 调 EventSink）。

    后续可再挂 Buffer capture / RuntimeThread.events() / 协议 notification 等订阅者——它们都只是
    同一条流的投影，互不调用。durable 持久化不在本流（已迁至 canonical 树）。
    """

    def __init__(self, sink) -> None:
        self._sink = sink       # UI EventSink

    def dispatch(self, event: RuntimeEvent) -> None:
        dispatch_event(event, self._sink)
