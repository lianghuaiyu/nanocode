"""tui/reducer.py —— 订阅事件流 → TuiState（docs/18）。

`reduce(state, env)` 把一条订阅信封（`{thread_id, session_id, seq, type, event}`，event 是 typed
AgentEvent，见 `agent/events.py`）归约进 TuiState，**原地更新并返回 state**。纯逻辑、无 UI 依赖——
on_event 在 `Agent.emit` 的同步扇出腿里调它，只改 state + `app.invalidate()`，绝不阻塞/不渲染。

spinner / cost **不是事件**而是 client 派生（docs/17）：`llm_request_prepared` → mode=running，
首个内容/终态事件落定；cost 从 `turn_completed.cost_usd` 取。assistant 流式：text/thinking 增量
合并进 timeline 末尾的「未收尾」同类项，`assistant_message_completed` 收尾。工具按 tool_use_id 关联
请求→结果。
"""

from __future__ import annotations

from typing import Any

from .state import (
    ApprovalModal,
    AssistantItem,
    ErrorItem,
    NoticeItem,
    StatusSnapshot,
    SubAgentItem,
    ThinkingItem,
    ToolItem,
    TuiState,
)

_RESULT_EXCERPT_CHARS = 240


def _g(event: Any, name: str, default: Any = None) -> Any:
    """事件字段读取（typed dataclass 或 dict 信封皆可）。"""
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def _open_item(state: TuiState, cls) -> Any:
    """timeline 末尾若是未收尾的 cls 项则返回它，否则 None（assistant/thinking 增量合并用）。"""
    if state.timeline:
        last = state.timeline[-1]
        if isinstance(last, cls) and not getattr(last, "complete", False):
            return last
    return None


def _find_tool(state: TuiState, tool_use_id: str) -> ToolItem | None:
    t = state.active_tools.get(tool_use_id)
    if t is not None:
        return t
    # 已出 active 表（已观测结果）的，回扫 timeline 兜底
    for item in reversed(state.timeline):
        if isinstance(item, ToolItem) and item.id == tool_use_id:
            return item
    return None


def reduce(state: TuiState, env: dict) -> TuiState:
    kind = env.get("type")
    event = env.get("event")

    if kind == "user_message_accepted":
        from .state import UserItem
        state.timeline.append(UserItem(text=_g(event, "text", "")))

    elif kind == "llm_request_prepared":
        state.mode = "running"
        model = _g(event, "model")
        if model:
            state.status.model = model

    elif kind == "assistant_delta":
        text = _g(event, "text", "") or ""
        thinking = _g(event, "thinking", "") or ""
        if thinking:
            it = _open_item(state, ThinkingItem)
            if it is None:
                it = ThinkingItem()
                state.timeline.append(it)
            it.text += thinking
        if text:
            it = _open_item(state, AssistantItem)
            if it is None:
                it = AssistantItem()
                state.timeline.append(it)
            it.text += text

    elif kind == "assistant_message_completed":
        # 收尾末尾未完成的 assistant/thinking 项；无增量但有文本时补建一条完成项。
        a = _open_item(state, AssistantItem)
        t = _open_item(state, ThinkingItem)
        if a is not None:
            a.complete = True
        if t is not None:
            t.complete = True
        if a is None and (_g(event, "text") or ""):
            state.timeline.append(AssistantItem(text=_g(event, "text", ""), complete=True))

    elif kind == "tool_call_requested":
        tid = _g(event, "tool_use_id", "") or ""
        item = ToolItem(id=tid, name=_g(event, "tool", ""), input=_g(event, "input", {}) or {})
        state.timeline.append(item)
        if tid:
            state.active_tools[tid] = item

    elif kind == "tool_result_observed":
        item = _find_tool(state, _g(event, "tool_use_id", "") or "")
        if item is not None:
            if item.status == "running":
                item.status = "done"
            item.chars = _g(event, "chars", 0) or 0
            item.result_excerpt = (_g(event, "result", "") or "")[:_RESULT_EXCERPT_CHARS]
            state.active_tools.pop(item.id, None)

    elif kind == "tool_result_completed":
        # durable 收口点：仅用于把 error 态补在 observed 之后（observed 无 is_error）。
        if _g(event, "is_error", False):
            item = _find_tool(state, _g(event, "tool_use_id", "") or "")
            if item is not None:
                item.status = "error"
            state.active_tools.pop(_g(event, "tool_use_id", "") or "", None)

    elif kind == "tool_call_authorized" and _g(event, "action") == "deny":
        item = _find_tool(state, _g(event, "tool_use_id", "") or "")
        msg = _g(event, "message") or ""
        if item is not None:
            item.status = "denied"
            item.result_summary = msg
            state.active_tools.pop(item.id, None)
        else:
            state.timeline.append(NoticeItem(text=f"Denied: {msg}", level="warn"))

    elif kind == "tool_blocked":
        state.timeline.append(
            NoticeItem(text=f"{_g(event, 'tool', '')} blocked: {_g(event, 'reason', '')}", level="warn")
        )

    elif kind == "budget_exceeded":
        state.timeline.append(NoticeItem(text=f"Budget exceeded: {_g(event, 'reason', '')}", level="warn"))

    elif kind == "notice_raised":
        state.timeline.append(NoticeItem(text=_g(event, "text", ""), level=_g(event, "level", "info")))

    elif kind == "retry_raised":
        state.timeline.append(
            NoticeItem(
                text=f"retry {_g(event, 'attempt', '')}/{_g(event, 'max_retries', '')}: {_g(event, 'reason', '')}",
                level="retry",
            )
        )

    elif kind == "compaction_requested":
        state.timeline.append(NoticeItem(text=f"Compacted context ({_g(event, 'reason', '')})", level="info"))

    elif kind == "sub_agent_started":
        state.timeline.append(
            SubAgentItem(agent_type=_g(event, "agent_type", ""), description=_g(event, "description", ""))
        )

    elif kind == "sub_agent_ended":
        at = _g(event, "agent_type", "")
        for item in reversed(state.timeline):
            if isinstance(item, SubAgentItem) and item.agent_type == at and item.status == "running":
                item.status = "done"
                break

    elif kind == "approval_requested":
        state.mode = "approval"
        state.modal = ApprovalModal(
            command=_g(event, "command", ""),
            message=_g(event, "message", ""),
            request_id=_g(event, "request_id", "") or "",
        )

    elif kind == "turn_completed":
        state.mode = "idle"
        state.modal = None
        state.status.input_tokens = _g(event, "input_tokens", state.status.input_tokens) or 0
        state.status.output_tokens = _g(event, "output_tokens", state.status.output_tokens) or 0
        cost = _g(event, "cost_usd")
        if cost is not None:
            state.status.cost_usd = cost
        state.active_tools.clear()

    elif kind == "turn_aborted":
        state.mode = "idle"
        state.modal = None
        state.timeline.append(NoticeItem(text="Interrupted — back to prompt.", level="warn"))
        state.active_tools.clear()

    elif kind == "error_raised":
        state.mode = "error"
        state.timeline.append(ErrorItem(text=_g(event, "message", "")))

    return state


def hydrate_status(state: TuiState, snapshot: dict) -> TuiState:
    """用 `RuntimeThread.status()`/`state()` 快照刷新 footer 状态（session 切换 / 周期刷新）。

    timeline-from-messages 的完整 re-hydrate 属后续步骤（接 `state().messages`）；此处只收口 footer。"""
    state.status = StatusSnapshot.from_status(snapshot)
    state.mode = "running" if state.status.is_processing else ("idle" if state.mode != "error" else "error")
    return state
