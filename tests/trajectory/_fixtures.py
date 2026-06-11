"""tests.trajectory._fixtures — 用真实 SessionManager 构造 canonical 树（Milestone B2）。

把旧 wire fixtures 替换为**真实树**：create(sid) → append user / assistant(usage+latencyMs+
toolCall blocks) / toolResult(latencyMs) / 派生遥测 entry（LLM_REQUEST / PERMISSION_DECISION /
TURN_END / SESSION_END / TOOL_BLOCKED / BUDGET_EXCEEDED / COMPACTION）。可选子会话（多 agent
fan-out，经 parentSession.agentId）。

这些 helper 复刻 engine 实际落树的形状（见 session/capture.py + session/tree.py），使
``trajectory._tree_events.tree_events`` 重建出与旧 wire 等价的事件流。
"""
from __future__ import annotations

from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def new_session(sid: str, *, lock: bool = True, cwd: str | None = None,
                parent_session: dict | None = None) -> SessionManager:
    """create 一个加锁的 canonical session（默认持写锁，供 append）。"""
    return SessionManager.create(sid, cwd=cwd, parent_session=parent_session, lock=lock)


def append_user(mgr: SessionManager, text: str):
    return mgr.append_message(T.user_message(text))


def append_llm_request(mgr: SessionManager, *, model: str = "claude-x",
                       message_count: int = 1, messages_chars: int = 42):
    """LLM_REQUEST 注解 entry（B1：llm_request sizing 落树）。"""
    return mgr.append(T.LLM_REQUEST, {
        "model": model, "messageCount": message_count, "messagesChars": messages_chars,
    })


def append_assistant(mgr: SessionManager, *, text: str = "", tool_calls: list | None = None,
                     input_tokens: int | None = None, output_tokens: int | None = None,
                     latency_ms: int | None = None, provider: str = "anthropic",
                     model: str = "claude-x", stop_reason: str = "stop"):
    """assistant MESSAGE，content = text 块 + 每个 tool_call 一个 toolCall 块。

    tool_calls: list[dict] of {"id","name","arguments"}. usage 用 camelCase（与树盘上一致）。
    """
    content: list[dict] = []
    if text:
        content.append(T.text_block(text))
    for tc in (tool_calls or []):
        content.append(T.tool_call_block(tc["id"], tc["name"], tc.get("arguments", {})))
    usage = None
    if input_tokens is not None or output_tokens is not None:
        usage = {"inputTokens": input_tokens or 0, "outputTokens": output_tokens or 0}
    msg = T.assistant_message(
        content, provider=provider, api=provider, model=model,
        stop_reason=stop_reason, usage=usage, latency_ms=latency_ms,
    )
    return mgr.append_message(msg)


def append_tool_result(mgr: SessionManager, *, tool_call_id: str, tool_name: str,
                       content: str, is_error: bool = False, latency_ms: int | None = None):
    msg = T.tool_result_message(
        tool_call_id=tool_call_id, tool_name=tool_name, content=content,
        is_error=is_error, latency_ms=latency_ms,
    )
    return mgr.append_message(msg)


def append_permission(mgr: SessionManager, *, tool: str, action: str, message: str = ""):
    return mgr.append(T.PERMISSION_DECISION, {"tool": tool, "action": action, "message": message})


def append_tool_blocked(mgr: SessionManager, *, tool: str, reason: str = "not_in_allowlist"):
    return mgr.append(T.TOOL_BLOCKED, {"tool": tool, "reason": reason})


def append_budget_exceeded(mgr: SessionManager, *, reason: str):
    return mgr.append(T.BUDGET_EXCEEDED, {"reason": reason})


def append_turn_end(mgr: SessionManager, *, input_tokens: int = 0, output_tokens: int = 0,
                    turns: int = 1, final_status: str = "completed"):
    return mgr.append(T.TURN_END, {
        "inputTokens": input_tokens, "outputTokens": output_tokens,
        "turns": turns, "finalStatus": final_status,
    })


def append_session_end(mgr: SessionManager, *, final_status: str = "completed"):
    return mgr.append(T.SESSION_END, {"finalStatus": final_status})


def append_compaction(mgr: SessionManager, *, kind: str = "auto",
                      before: int = 40, after: int = 12):
    return mgr.append(T.COMPACTION, {
        "kind": kind, "messageCountBefore": before, "messageCountAfter": after,
        "summary": "summarized",
    })


def child_session(parent: SessionManager, child_sid: str, *, agent_id: str) -> SessionManager:
    """create 一个 child session，header 回指 parent + agentId（多 agent fan-out）。

    children(parent_sid) 经 header.parentSession.sessionId 扫描发现；agent_id 经
    parent_session().agentId 恢复。
    """
    return SessionManager.create(
        child_sid,
        parent_session={"sessionId": parent.session_id, "entryId": parent.get_leaf(),
                        "agentId": agent_id},
        lock=True,
    )
