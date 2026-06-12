"""runtime_events — typed AgentEvent 的 UI 投影（docs/16 #2/#4）。

string-channel 时代（RuntimeEvent + DURABLE_TYPES/EPHEMERAL_UI_TYPES 静态分类表 + EventDispatcher）
已随 docs/16 #4 整体删除：live 路径全 typed——`Agent.emit(AgentEvent)` 单出口扇出
`[record_event(canonical 树), project_agent_event(UI), thread push 订阅者]`。
durable 持久化通道见 events.DURABLE_ENTRY_FOR_EVENT（additive 契约；trajectory 从树派生）。
"""

from __future__ import annotations


def project_agent_event(event, sink) -> None:
    """typed AgentEvent → EventSink UI 投影（`Agent.emit` 单扇出的 ui 腿）。

    有 UI 的只有三种：AssistantDelta（流式 text/thinking 逐 block）、ToolCallRequested、
    ToolResultObserved（执行点即时观测）。其余事件的持久化等价物由 record_event 落 canonical 树，
    UI 一律 no-op——尤其 AssistantMessageCompleted 整段**不**重复渲染（流式已逐 block 画过）。
    """
    k = getattr(event, "kind", None)
    if k == "assistant_delta":
        if event.text:
            sink.assistant_markdown(event.text)
        if event.thinking:
            sink.thinking(event.thinking)
    elif k == "tool_call_requested":
        sink.tool_call(event.tool, event.input)
    elif k == "tool_result_observed":
        sink.tool_result(event.tool, event.result)
