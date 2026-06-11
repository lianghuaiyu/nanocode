"""agent/events.py — typed AgentEvent union（docs/15 §6）。

`AgentCore.run_turn(state, request) -> AsyncIterator[AgentEvent]` 发出这些事件；`AgentSession`
把它们转成 canonical session entries / telemetry entries / UI 投影——取代旧的**双发**
（`_dispatch_event` 的 RuntimeEvent + `_tree_event` / `_tree_record` / `_tree_custom_message`）。

与 `runtime_events.DURABLE_EVENT_FIELDS` 的 **additive 契约**对齐（trajectory 从 canonical 树派生，
docs/14 B2）：事件字段只增不改名/不删。每个事件携带把它落成 session entry 所需的全部中立事实
（如 AssistantMessageCompleted.message 已是中立 Message dict，可直接 append_message）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class UserMessageAccepted:
    """用户输入被接受（在模型请求前；AgentSession 据此 append_message required=True）。"""

    text: str
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
    """权限决策结果（落 PERMISSION_DECISION 遥测 entry）。action ∈ allow|confirm|deny。"""

    tool: str
    tool_use_id: str
    action: str
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
    """call-time allowlist fail-closed 拦截（落 TOOL_BLOCKED 遥测 entry）。"""

    tool: str
    reason: str
    kind: str = "tool_blocked"


@dataclass(frozen=True)
class CompactionRequested:
    """请求压缩（AgentSession.compact 写 COMPACTION entry，两区 fold）。"""

    reason: str
    tokens_before: int | None = None
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
    """turn 正常收尾（落 TURN_END 遥测 entry，finalStatus=completed）。"""

    input_tokens: int
    output_tokens: int
    turns: int
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


# 整个 union（用于 isinstance fan-out / 类型注解）。
AgentEvent = Union[
    UserMessageAccepted,
    LlmRequestPrepared,
    AssistantDelta,
    AssistantMessageCompleted,
    ToolCallRequested,
    ToolCallAuthorized,
    ToolResultCompleted,
    ToolBlocked,
    CompactionRequested,
    ContextInjected,
    TurnCompleted,
    TurnAborted,
    ErrorRaised,
]

ALL_AGENT_EVENTS: tuple[type, ...] = (
    UserMessageAccepted, LlmRequestPrepared, AssistantDelta, AssistantMessageCompleted,
    ToolCallRequested, ToolCallAuthorized, ToolResultCompleted, ToolBlocked,
    CompactionRequested, ContextInjected, TurnCompleted, TurnAborted, ErrorRaised,
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
    "tool_blocked": "tool_blocked",
    "compaction_requested": "compaction",
    "context_injected": "custom_message",
    "turn_completed": "turn_end",
    "turn_aborted": "turn_end",
    "error_raised": None,
}
