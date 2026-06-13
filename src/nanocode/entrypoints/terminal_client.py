"""TerminalClient —— 订阅 RuntimeThread 事件流、渲染到终端的客户端（docs/17 Phase 1）。

设计：TUI 不是 agent core 的一部分，而是挂在 runtime/session 上、订阅 typed AgentEvent 流的
一个 client（对齐 pi 的 `session.subscribe(handleEvent)`）。core 只 `emit(AgentEvent)`，渲染
全在订阅端。

Phase 1 接管三类**流式渲染**事件（assistant 文本/thinking、工具调用、工具结果）——它们此前经
`runtime_events.project_agent_event` 在 `Agent.emit` 内同步投影到 EventSink。spinner / cost /
info / retry / sub_agent 仍经 EventSink 直渲，待 Phase 2 升格为 typed 事件后一并迁入本 client。

订阅信封格式（RuntimeThread._envelope）：`{thread_id, session_id, seq, type, event}`，其中
`event` 是 typed AgentEvent（或边界 dict，如 session_switch）。`on_event` 在 `Agent.emit` 的
订阅者扇出腿内**同步**被调用（树写之后）——与旧 project_agent_event 的时序一致：spinner 经
StreamCallbacks 在首 block 前已停，故渲染不与 spinner 行交错。
"""

from __future__ import annotations

from .. import ui


class TerminalClient:
    """把 thread 事件流渲染到终端。无状态（Phase 1）；经 `thread.subscribe(client.on_event)` 挂载。"""

    def on_event(self, env: dict) -> None:
        kind = env.get("type")
        event = env.get("event")
        if kind == "assistant_delta":
            # 流式增量：text → markdown gutter，thinking → dim italic（仅 text block 进 final 捕获）。
            if getattr(event, "text", ""):
                ui.render_assistant_markdown(event.text)
            if getattr(event, "thinking", ""):
                ui.render_thinking(event.thinking)
        elif kind == "tool_call_requested":
            ui.print_tool_call(event.tool, event.input)
        elif kind == "tool_result_observed":
            ui.print_tool_result(event.tool, event.result)
