"""agent/events.py — typed AgentEvent union（docs/15 §6）。

AgentCore / engine 经 **单一出口 `Agent.emit(event)`** 发出这些事件（docs/16 #2），扇出
`[AgentSession.record_event（canonical 树）, _event_subscribers（订阅者 push）]`
——旧的**双发**（`_dispatch_event` 的 RuntimeEvent + `_tree_event` / `_tree_record` /
`_tree_custom_message` 各自直调）已被取代。docs/17 Phase 1：UI 投影腿（project_agent_event）
已删，assistant/tool 的渲染改由订阅端 TerminalClient 从事件流派生。

**additive 契约**（trajectory 从 canonical 树派生，docs/14 B2；持久化通道映射见
DURABLE_ENTRY_FOR_EVENT）：事件字段只增不改名/不删。每个事件携带把它落成 session entry 所需的全部中立事实
（如 AssistantMessageCompleted.message 已是中立 Message dict，可直接 append_message）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class UserMessageAccepted:
    """用户输入被接受（在模型请求前；AgentSession 据此 append_message required=True）。

    message（additive，docs/16 #0）：已 capture 的中立 user Message；非 None 时 record_event 直接
    append（保留 block content/timestamp 语义），None 时退回 text 重建（纯文本场景等价）。"""

    text: str
    message: dict | None = None
    kind: str = "user_message_accepted"


@dataclass(frozen=True)
class LlmRequestPrepared:
    """一次 provider 请求已组装（落 LLM_REQUEST 遥测 entry：sizing 事后不可重建，须 emit-time 捕获）。"""

    model: str
    message_count: int
    messages_chars: int
    kind: str = "llm_request_prepared"


@dataclass(frozen=True)
class AssistantDelta:
    """流式增量（text / thinking 逐 block）。**仅 UI 投影**，无持久化等价物。"""

    text: str = ""
    thinking: str = ""
    kind: str = "assistant_delta"


@dataclass(frozen=True)
class AssistantMessageCompleted:
    """一条 assistant 消息收尾。message 是中立 Message dict（含真实 stopReason/usage/latency），
    AgentSession 直接 append_message（required=True，docs/15 §7.6②：删 flat 后必须 fail-loud）。"""

    message: dict
    text: str
    thinking: str
    tool_uses: list[dict]
    stop_reason: str | None
    usage: dict | None
    latency_ms: int | None
    kind: str = "assistant_message_completed"


@dataclass(frozen=True)
class ToolCallRequested:
    tool: str
    input: dict
    tool_use_id: str
    kind: str = "tool_call_requested"


@dataclass(frozen=True)
class ToolCallAuthorized:
    """权限决策结果（落 PERMISSION_DECISION 遥测 entry）。action ∈ allow|confirm|deny。
    tool_use_id 可空（_authorize_dispatch 的决策点只见 (name, input)；树 entry 历来不含它）。"""

    tool: str
    action: str
    tool_use_id: str = ""
    message: str | None = None
    kind: str = "tool_call_authorized"


@dataclass(frozen=True)
class ToolResultCompleted:
    """工具结果。message 是中立 toolResult Message dict（AgentSession append_message，required=True）。"""

    message: dict
    tool: str
    tool_use_id: str
    content: str
    is_error: bool
    latency_ms: int | None
    kind: str = "tool_result_completed"


@dataclass(frozen=True)
class ToolBlocked:
    """call-time allowlist fail-closed 拦截（落 TOOL_BLOCKED 遥测 entry；agentType/artifactId
    由 record_event 从 agent 身份补齐——事件本身只携带拦截事实）。"""

    tool: str
    reason: str
    kind: str = "tool_blocked"


@dataclass(frozen=True)
class ToolResultObserved:
    """工具执行完成的**即时观测**（逐工具、执行点实时发出；**仅 UI 投影**）。

    durable 等价物是批量 toolResult 消息在收口点拆出的 ToolResultCompleted——观测在执行点、
    树写在批量消息收口点，两个时刻本就不同（docs/16 #1 锁定的 inline 顺序），故是两个事件。"""

    tool: str
    tool_use_id: str
    chars: int
    result: str
    kind: str = "tool_result_observed"


@dataclass(frozen=True)
class BudgetExceeded:
    """成本/turn 预算触顶，模型循环终止（落 BUDGET_EXCEEDED 遥测 entry）。"""

    reason: str
    kind: str = "budget_exceeded"


@dataclass(frozen=True)
class CompactionRequested:
    """压缩事实（AgentSession.compact 产出 summary 后发出；record_event 写 COMPACTION entry，
    两区 fold）。additive（docs/16 #3a）：携带把它落成 entry 所需的全部事实——summary /
    first_kept_entry_id / 前后计数（neutral 消息数，前 = fold 现状、后 = 含 pending compaction
    的合成 fold 预测）。compaction_kind ∈ auto|manual（落 entry 的 kind 字段）。"""

    reason: str
    tokens_before: int | None = None
    summary: str | None = None
    first_kept_entry_id: str | None = None
    message_count_before: int | None = None
    message_count_after: int | None = None
    compaction_kind: str = "auto"
    kind: str = "compaction_requested"


@dataclass(frozen=True)
class ContextInjected:
    """一个 ContextPack 被注入（AgentSession 写 custom_message entry；display=False，对 LLM 可见）。"""

    custom_type: str
    content: Any
    pack_id: str | None = None
    kind: str = "context_injected"


@dataclass(frozen=True)
class TurnCompleted:
    """turn 正常收尾（落 TURN_END 遥测 entry，finalStatus=completed）。

    cost_usd（additive，docs/17 Phase 2）：emit 时算好的累计美元成本，供订阅客户端（含 RPC headless）
    直接显示而无需自带定价表；None 表示未知（无定价）。"""

    input_tokens: int
    output_tokens: int
    turns: int
    cost_usd: float | None = None
    kind: str = "turn_completed"


@dataclass(frozen=True)
class TurnAborted:
    """turn 被取消（落 TURN_END finalStatus=cancelled；§7.6② aborted assistant 须标记可丢）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    kind: str = "turn_aborted"


@dataclass(frozen=True)
class ErrorRaised:
    """turn 内不可恢复错误（归一上抛，不外泄崩溃父循环）。"""

    message: str
    kind: str = "error_raised"


# ─── UI-only 事件（docs/17 Phase 2：从 EventSink 直渲迁到 typed 事件流）──────────
# 这些无持久化等价物（DURABLE_ENTRY_FOR_EVENT=None）：旧经 self._sink.<info/retry/sub_agent_*>
# 直接驱动表现层，现升格为事件、由订阅端 TerminalClient 渲染——core 只 emit、不再认识表现层。

@dataclass(frozen=True)
class NoticeRaised:
    """自由文本诊断/状态通知（取代散落的 self._sink.info）。level ∈ info|warn。

    仅用于没有更具体 typed 事件的人面消息（overflow-retry、压缩状态、tree/state 持久化告警、
    session 切换、MCP 连接日志等）。已有 typed 事件的（BudgetExceeded、ToolCallAuthorized deny）
    一律渲染那些事件，绝不再走 NoticeRaised——避免它退化成 info sink 的别名。"""

    text: str
    level: str = "info"
    kind: str = "notice_raised"


@dataclass(frozen=True)
class RetryRaised:
    """provider 流重试通知（旧 self._sink.retry，经 StreamCallbacks.retry 触发）。"""

    attempt: int
    max_retries: int
    reason: str
    kind: str = "retry_raised"


@dataclass(frozen=True)
class SubAgentStarted:
    """子 agent / skill-fork 开始（旧 host._sink.sub_agent_start）。父 agent 的事件流可见。"""

    agent_type: str
    description: str
    kind: str = "sub_agent_started"


@dataclass(frozen=True)
class SubAgentEnded:
    """子 agent / skill-fork 结束（旧 host._sink.sub_agent_end）。"""

    agent_type: str
    description: str
    kind: str = "sub_agent_ended"


@dataclass(frozen=True)
class ApprovalRequested:
    """危险动作待审批（旧 self._sink.confirmation）——**显示**事件，订阅端据此渲染告警。
    实际决策仍经注入的 confirm_fn 往返（interactive：读 y/n；RPC：stdout 请求 + stdin 响应）。
    request_id 为 RPC 关联键（interactive 不需要，默认空）。"""

    command: str
    message: str
    request_id: str = ""
    kind: str = "approval_requested"


# 整个 union（用于 isinstance fan-out / 类型注解）。
AgentEvent = Union[
    UserMessageAccepted,
    LlmRequestPrepared,
    AssistantDelta,
    AssistantMessageCompleted,
    ToolCallRequested,
    ToolCallAuthorized,
    ToolResultCompleted,
    ToolResultObserved,
    ToolBlocked,
    BudgetExceeded,
    CompactionRequested,
    ContextInjected,
    TurnCompleted,
    TurnAborted,
    ErrorRaised,
    NoticeRaised,
    RetryRaised,
    SubAgentStarted,
    SubAgentEnded,
    ApprovalRequested,
]

ALL_AGENT_EVENTS: tuple[type, ...] = (
    UserMessageAccepted, LlmRequestPrepared, AssistantDelta, AssistantMessageCompleted,
    ToolCallRequested, ToolCallAuthorized, ToolResultCompleted, ToolResultObserved,
    ToolBlocked, BudgetExceeded,
    CompactionRequested, ContextInjected, TurnCompleted, TurnAborted, ErrorRaised,
    NoticeRaised, RetryRaised, SubAgentStarted, SubAgentEnded, ApprovalRequested,
)

# 事件 kind → 对应的 canonical 树 entry 持久化通道（None = 仅 UI / 无树等价物）。
# AgentSession.record_event 据此把事件转成 session entry；trajectory 从这些 entry 派生（B2）。
# 与 tree.py 的常量对齐（避免循环 import，这里只用字符串字面量）。
DURABLE_ENTRY_FOR_EVENT: dict[str, str | None] = {
    "user_message_accepted": "message",
    "llm_request_prepared": "llm_request",
    "assistant_delta": None,
    "assistant_message_completed": "message",
    "tool_call_requested": None,
    "tool_call_authorized": "permission_decision",
    "tool_result_completed": "message",
    "tool_result_observed": None,
    "tool_blocked": "tool_blocked",
    "budget_exceeded": "budget_exceeded",
    "compaction_requested": "compaction",
    "context_injected": "custom_message",
    "turn_completed": "turn_end",
    "turn_aborted": "turn_end",
    "error_raised": None,
    # docs/17 Phase 2：UI-only 事件——无树等价物，由 TerminalClient 渲染。
    "notice_raised": None,
    "retry_raised": None,
    "sub_agent_started": None,
    "sub_agent_ended": None,
    "approval_requested": None,
}


# ─── capture-at-emit（docs/16 #0）───────────────────────────────────────────
#
# STEP D 的唯一致命陷阱拆雷：AgentCore 持有的 message 是 **provider-shaped**（anthropic block
# dicts / openai message dicts），而 AgentSession.record_event._append_neutral 假设 event.message
# 已是**中立 Message**（绕过 capture——中立再 capture 会丢 toolCall 块）。因此转换必须发生在
# **emit 时**（AgentCore 知道 provider 形状）。本工厂与 engine._tree_record 的内联 capture 路径
# **逐字节等价**（同一 neutral_stop_reason 映射、同一 capture_anthropic/capture_openai），由
# tests/agent/test_capture_at_emit.py 的树级 parity 测试锚定。

def events_from_provider_message(
    provider_msg: dict,
    *,
    provider: str,
    model: str,
    stop_reason: "str | None" = None,
    usage: "dict | None" = None,
    latency_ms: "int | None" = None,
) -> list["AgentEvent"]:
    """一条 provider-shaped 消息 → 0+ 条 message-family AgentEvent（capture-at-emit）。

    入参语义与 `engine._tree_record` 一致：`stop_reason` 是 provider **原生**值（此处映射成
    中立值再交 capture）；`usage`/`latency_ms` 透传。产出事件的 `.message` 即中立 Message，
    可直接 `record_event`（required=True append）。

    条数语义沿袭 capture：anthropic 的 tool_result-user 批量消息拆成 N 条 ToolResultCompleted；
    openai 的 system 消息产 0 条；assistant 恒 1 条。
    """
    import json as _json

    from ..session import capture as _capture

    neutral_sr = _capture.neutral_stop_reason(provider, stop_reason)
    cap = _capture.capture_openai if provider == "openai" else _capture.capture_anthropic
    out: list[AgentEvent] = []
    for m in cap(provider_msg, model=model, stop_reason=neutral_sr,
                 usage=usage, latency_ms=latency_ms):
        role = m.get("role")
        if role == "user":
            c = m.get("content")
            out.append(UserMessageAccepted(text=c if isinstance(c, str) else "", message=m))
        elif role == "assistant":
            blocks = m.get("content") or []
            text = "".join(b.get("text", "") for b in blocks
                           if isinstance(b, dict) and b.get("type") == "text")
            thinking = "".join(b.get("thinking", "") for b in blocks
                               if isinstance(b, dict) and b.get("type") == "thinking")
            tool_uses = [{"id": b.get("id"), "name": b.get("name"), "input": b.get("arguments")}
                         for b in blocks if isinstance(b, dict) and b.get("type") == "toolCall"]
            out.append(AssistantMessageCompleted(
                message=m, text=text, thinking=thinking, tool_uses=tool_uses,
                stop_reason=m.get("stopReason"), usage=m.get("usage"),
                latency_ms=m.get("latencyMs")))
        elif role == "toolResult":
            c = m.get("content")
            content = c if isinstance(c, str) else _json.dumps(c, ensure_ascii=False, default=str)
            out.append(ToolResultCompleted(
                message=m, tool=m.get("toolName", ""), tool_use_id=m.get("toolCallId", ""),
                content=content, is_error=bool(m.get("isError", False)),
                latency_ms=m.get("latencyMs")))
    return out
