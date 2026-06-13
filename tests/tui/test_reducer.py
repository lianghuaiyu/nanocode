"""tests/tui/test_reducer.py —— reducer 把订阅事件流归约进 TuiState（docs/18 step 1）。

用真 typed AgentEvent（agent/events.py）构信封驱动，断言 ViewModel 转换——这是 docs/17 刻意延后、
docs/18 引入的 ViewModel 层的回归地基。
"""

from __future__ import annotations

from nanocode.agent import events as E
from nanocode.tui import reducer
from nanocode.tui.state import (
    AssistantItem,
    ErrorItem,
    NoticeItem,
    SubAgentItem,
    ThinkingItem,
    ToolItem,
    TuiState,
    UserItem,
)


def env(event):
    """订阅信封：{thread_id, session_id, seq, type, event}（reducer 只读 type/event）。"""
    return {"thread_id": "t", "session_id": "s", "seq": 0, "type": event.kind, "event": event}


def drive(*ev):
    st = TuiState()
    for e in ev:
        reducer.reduce(st, env(e))
    return st


# ─── user / assistant streaming ────────────────────────────


def test_user_message_appends_user_item():
    st = drive(E.UserMessageAccepted(text="hello"))
    assert len(st.timeline) == 1 and isinstance(st.timeline[0], UserItem)
    assert st.timeline[0].text == "hello"


def test_llm_request_sets_running_and_model():
    st = drive(E.LlmRequestPrepared(model="claude-opus-4-8", message_count=3, messages_chars=120))
    assert st.mode == "running"
    assert st.status.model == "claude-opus-4-8"


def test_assistant_text_deltas_merge_into_one_item():
    st = drive(E.AssistantDelta(text="Hel"), E.AssistantDelta(text="lo"), E.AssistantDelta(text=" world"))
    assistants = [i for i in st.timeline if isinstance(i, AssistantItem)]
    assert len(assistants) == 1
    assert assistants[0].text == "Hello world"
    assert assistants[0].complete is False


def test_thinking_and_text_are_separate_items():
    st = drive(E.AssistantDelta(thinking="ponder"), E.AssistantDelta(text="answer"))
    kinds = [type(i) for i in st.timeline]
    assert ThinkingItem in kinds and AssistantItem in kinds


def test_completed_marks_open_items_complete():
    st = drive(E.AssistantDelta(text="hi"))
    reducer.reduce(
        st,
        env(E.AssistantMessageCompleted(message={}, text="hi", thinking="", tool_uses=[], stop_reason="end", usage=None, latency_ms=10)),
    )
    a = [i for i in st.timeline if isinstance(i, AssistantItem)][0]
    assert a.complete is True


def test_completed_without_deltas_synthesizes_item():
    st = TuiState()
    reducer.reduce(
        st,
        env(E.AssistantMessageCompleted(message={}, text="final", thinking="", tool_uses=[], stop_reason="end", usage=None, latency_ms=1)),
    )
    a = [i for i in st.timeline if isinstance(i, AssistantItem)]
    assert len(a) == 1 and a[0].text == "final" and a[0].complete is True


# ─── tool lifecycle ────────────────────────────────────────


def test_tool_request_creates_running_item_in_active_and_timeline():
    st = drive(E.ToolCallRequested(tool="Read", input={"path": "a.py"}, tool_use_id="tu1"))
    item = st.active_tools["tu1"]
    assert isinstance(item, ToolItem) and item.status == "running" and item.name == "Read"
    assert item in st.timeline


def test_tool_result_observed_marks_done_and_leaves_active():
    st = drive(
        E.ToolCallRequested(tool="Read", input={}, tool_use_id="tu1"),
        E.ToolResultObserved(tool="Read", tool_use_id="tu1", chars=42, result="file body"),
    )
    item = [i for i in st.timeline if isinstance(i, ToolItem)][0]
    assert item.status == "done"
    assert item.chars == 42
    assert item.result_excerpt == "file body"
    assert "tu1" not in st.active_tools


def test_tool_result_completed_error_marks_error():
    st = drive(
        E.ToolCallRequested(tool="Bash", input={}, tool_use_id="tu2"),
        E.ToolResultObserved(tool="Bash", tool_use_id="tu2", chars=3, result="err"),
        E.ToolResultCompleted(message={}, tool="Bash", tool_use_id="tu2", content="boom", is_error=True, latency_ms=5),
    )
    item = [i for i in st.timeline if isinstance(i, ToolItem)][0]
    assert item.status == "error"


def test_tool_deny_marks_denied_when_tool_present():
    st = drive(
        E.ToolCallRequested(tool="Bash", input={}, tool_use_id="tu3"),
        E.ToolCallAuthorized(tool="Bash", action="deny", tool_use_id="tu3", message="not allowed"),
    )
    item = [i for i in st.timeline if isinstance(i, ToolItem)][0]
    assert item.status == "denied" and item.result_summary == "not allowed"
    assert "tu3" not in st.active_tools


def test_tool_deny_without_known_tool_appends_notice():
    st = drive(E.ToolCallAuthorized(tool="Bash", action="deny", tool_use_id="ghost", message="nope"))
    notices = [i for i in st.timeline if isinstance(i, NoticeItem)]
    assert notices and "Denied: nope" in notices[0].text


def test_tool_blocked_appends_warn_notice():
    st = drive(E.ToolBlocked(tool="Write", reason="not in allowlist"))
    n = [i for i in st.timeline if isinstance(i, NoticeItem)][0]
    assert n.level == "warn" and "Write" in n.text and "allowlist" in n.text


# ─── notices / retries / sub-agents ────────────────────────


def test_notice_and_retry_and_budget():
    st = drive(
        E.NoticeRaised(text="heads up", level="info"),
        E.RetryRaised(attempt=2, max_retries=5, reason="overflow"),
        E.BudgetExceeded(reason="cost cap"),
    )
    levels = {i.level for i in st.timeline if isinstance(i, NoticeItem)}
    assert {"info", "retry", "warn"} <= levels
    assert any("2/5" in i.text for i in st.timeline if isinstance(i, NoticeItem))


def test_sub_agent_start_then_end_marks_done():
    st = drive(
        E.SubAgentStarted(agent_type="Explore", description="scan"),
        E.SubAgentEnded(agent_type="Explore", description="scan"),
    )
    sa = [i for i in st.timeline if isinstance(i, SubAgentItem)][0]
    assert sa.status == "done"


# ─── approval / turn end / error ───────────────────────────


def test_approval_opens_modal_then_turn_completed_clears_it():
    st = drive(E.ApprovalRequested(command="pytest -q", message="Bash wants to run", request_id="r1"))
    assert st.mode == "approval"
    assert st.modal is not None and st.modal.request_id == "r1" and st.modal.command == "pytest -q"
    reducer.reduce(st, env(E.TurnCompleted(input_tokens=100, output_tokens=20, turns=1, cost_usd=0.0034)))
    assert st.mode == "idle" and st.modal is None
    assert st.status.input_tokens == 100 and st.status.output_tokens == 20
    assert st.status.cost_usd == 0.0034


def test_turn_aborted_resets_idle_and_notes_interrupt():
    st = drive(
        E.ToolCallRequested(tool="Read", input={}, tool_use_id="x"),
        E.TurnAborted(input_tokens=5, output_tokens=1, turns=1),
    )
    assert st.mode == "idle"
    assert not st.active_tools
    assert any(isinstance(i, NoticeItem) and "Interrupted" in i.text for i in st.timeline)


def test_error_raised_sets_error_mode_and_item():
    st = drive(E.ErrorRaised(message="kaboom"))
    assert st.mode == "error"
    assert any(isinstance(i, ErrorItem) and i.text == "kaboom" for i in st.timeline)


# ─── status hydration ──────────────────────────────────────


def test_hydrate_status_from_snapshot():
    st = TuiState()
    snap = {
        "session_id": "abc123",
        "session_name": "TUI redesign",
        "cwd": "/repo",
        "model": "claude-opus-4-8",
        "input_tokens": 42000,
        "output_tokens": 8000,
        "cost_usd": 0.083,
        "context_window": 200000,
        "thinking": "high",
        "is_processing": True,
    }
    reducer.hydrate_status(st, snap)
    assert st.status.session_name == "TUI redesign"
    assert st.status.input_tokens == 42000
    assert st.status.context_window == 200000
    assert st.mode == "running"  # is_processing=True
