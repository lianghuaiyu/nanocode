"""TerminalClient —— 订阅 RuntimeThread 事件流、渲染到终端的客户端（docs/17 Phase 1-2）。

设计：TUI 不是 agent core 的一部分，而是挂在 runtime/session 上、订阅 typed AgentEvent 流的
一个 client（对齐 pi 的 `session.subscribe(handleEvent)`）。core 只 `emit(AgentEvent)`，渲染
全在订阅端——core 不再认识 rich / 终端 / spinner。

事件 → 渲染映射：
- assistant_delta        → markdown gutter / thinking（流式逐 block）
- tool_call_requested    → 工具调用回显
- tool_result_observed   → 工具结果摘要
- notice_raised          → ℹ 通知（旧 sink.info）
- retry_raised           → ↻ 重试（旧 sink.retry）
- sub_agent_started/ended→ Task[...] 起止（旧 sink.sub_agent_*）
- budget_exceeded        → 预算触顶通知
- tool_call_authorized   → action=deny 时显示 Denied（旧 sink.info("Denied: ...")）
- turn_completed         → 成本行（旧 sink.cost；ui.print_cost 仅 verbose 下输出）

spinner **client 派生**（旧 core 的 cfg.sink.spinner_start/stop）：llm_request_prepared 起，
首个内容/终态事件停。唯一时序偏移（可接受、对标 sink.py 旧记录）：旧 StreamCallbacks.spinner_stop
在首 token 即停，本实现在首个 AssistantDelta（block 粒度）停——略晚、纯视觉、无语义影响。

订阅信封：`{thread_id, session_id, seq, type, event}`，`on_event` 在 `Agent.emit` 的订阅扇出腿内
**同步**被调用（树写之后）。spinner 在渲染任何内容前先停，故不与 spinner 行交错。
"""

from __future__ import annotations

from .. import ui

# 触发「停 spinner」的事件 kind：首个内容到达 / turn 终态 / 需要打印的通知。
# 不含 retry_raised——重试发生在同一 stream_fn 调用内、无后续 LlmRequestPrepared 重启 spinner，
# 故重试期间保留 spinner（与旧 print_retry-during-spinner 行为一致）。
_STOP_SPINNER_KINDS = frozenset({
    "assistant_delta", "tool_call_requested", "tool_result_observed",
    "turn_completed", "turn_aborted", "error_raised", "budget_exceeded",
    "notice_raised", "sub_agent_started", "approval_requested",
})


class TerminalClient:
    """把 thread 事件流渲染到终端；经 `thread.subscribe(client.on_event)` 挂载。

    spinner 是唯一状态（ui 模块级单例，本 client 是唯一驱动者）。无其它会话态——thread 替换时
    host 重新订阅即可，client 实例可复用。"""

    def on_event(self, env: dict) -> None:
        kind = env.get("type")
        event = env.get("event")

        if kind == "llm_request_prepared":
            ui.start_spinner()
            return

        if kind in _STOP_SPINNER_KINDS:
            ui.stop_spinner()   # 渲染任何内容前先停 spinner，避免与 spinner 行交错（幂等）

        if kind == "assistant_delta":
            if getattr(event, "text", ""):
                ui.render_assistant_markdown(event.text)
            if getattr(event, "thinking", ""):
                ui.render_thinking(event.thinking)
        elif kind == "tool_call_requested":
            ui.print_tool_call(event.tool, event.input)
        elif kind == "tool_result_observed":
            ui.print_tool_result(event.tool, event.result)
        elif kind == "notice_raised":
            ui.print_info(event.text)
        elif kind == "retry_raised":
            ui.print_retry(event.attempt, event.max_retries, event.reason)
        elif kind == "sub_agent_started":
            ui.print_sub_agent_start(event.agent_type, event.description)
        elif kind == "sub_agent_ended":
            ui.print_sub_agent_end(event.agent_type, event.description)
        elif kind == "budget_exceeded":
            ui.print_info(f"Budget exceeded: {event.reason}")
        elif kind == "tool_call_authorized" and getattr(event, "action", None) == "deny":
            ui.print_info(f"Denied: {event.message}")
        elif kind == "approval_requested":
            ui.print_confirmation(event.message)   # 显示告警；y/n 决策经注入的 confirm_fn
        elif kind == "turn_completed":
            ui.print_cost(event.input_tokens, event.output_tokens)
