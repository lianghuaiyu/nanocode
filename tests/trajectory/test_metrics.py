"""tests for trajectory.metrics.compute_metrics（P3 harness 指标聚合，docs/14 Milestone B2）。

构造 TrajEvent 列表（树适配器重建的事件等价物），断言每个指标的计数/比率，并覆盖
summary / malformed 容忍与 per_agent / per_tool breakdown。延迟优先取事件 data 上显式的
``latency_ms``（毫秒级，树适配器从 message.usage/latencyMs 取真值）。
"""
from __future__ import annotations

from nanocode.trajectory._tree_events import TrajEvent
from nanocode.trajectory.metrics import compute_metrics
from nanocode.trajectory.schema import Step


def _ev(etype, *, agent_id="main", seq=0, ts=None, **payload) -> TrajEvent:
    """构造一条 TrajEvent（payload 落到 .data）。"""
    if ts is None:
        ts = f"2026-06-09T00:00:{seq:02d}+00:00"
    return TrajEvent(
        type=etype, agent_id=agent_id, seq=seq, ts=ts, session_id="s1",
        id=f"evt_{agent_id}_{seq}", line_no=seq, data=dict(payload),
    )


def test_empty_events_all_zero():
    m = compute_metrics([])
    assert m["total_turns"] == 0
    assert m["total_tool_calls"] == 0
    assert m["tool_failure_count"] == 0
    assert m["tool_failure_rate"] == 0.0
    assert m["deny_rate"] == 0.0
    assert m["est_cost_usd"] == 0.0
    assert m["files_touched"] == []
    assert m["per_agent"] == {}
    assert m["per_tool"] == {}
    assert m["high_risk_action_count"] == 0


def test_none_events_does_not_crash():
    m = compute_metrics(None)
    assert m["total_tool_calls"] == 0


def test_turns_and_tokens_and_cost():
    events = [
        _ev("llm_request", seq=0, model="m"),
        _ev("llm_response", seq=1, input_tokens=1_000_000, output_tokens=2_000_000),
        _ev("turn_end", seq=2, turns=1),
        _ev("llm_request", seq=3),
        _ev("llm_response", seq=4, input_tokens=0, output_tokens=0),
        _ev("turn_end", seq=5),
    ]
    m = compute_metrics(events)
    assert m["total_turns"] == 2
    assert m["total_input_tokens"] == 1_000_000
    assert m["total_output_tokens"] == 2_000_000
    assert abs(m["est_cost_usd"] - 33.0) < 1e-9


def test_tool_calls_and_failure_rate():
    events = [
        _ev("tool_call", seq=0, tool="read_file", input={"file_path": "/a.py"}, tool_use_id="t0"),
        _ev("tool_result", seq=1, tool="read_file", tool_use_id="t0", result="file contents"),
        _ev("tool_call", seq=2, tool="run_shell", input={"command": "ls"}, tool_use_id="t1"),
        _ev("tool_result", seq=3, tool="run_shell", tool_use_id="t1", result="Error: boom"),
        _ev("tool_call", seq=4, tool="grep_search", input={}, tool_use_id="t2"),
        _ev("tool_result", seq=5, tool="grep_search", tool_use_id="t2", result="Warning: nothing matched"),
    ]
    m = compute_metrics(events)
    assert m["total_tool_calls"] == 3
    assert m["tool_failure_count"] == 2
    assert abs(m["tool_failure_rate"] - (2 / 3)) < 1e-9


def test_failure_prefix_is_case_insensitive_and_leading_ws():
    events = [
        _ev("tool_call", seq=0, tool="x", input={}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="x", tool_use_id="a", result="  ERROR: nope"),
    ]
    m = compute_metrics(events)
    assert m["tool_failure_count"] == 1


def test_summary_level_result_uses_result_summary():
    events = [
        _ev("tool_call", seq=0, tool="run_shell", input={"command": "x"}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="run_shell", tool_use_id="a",
            result_summary="Error: failed", result_hash="sha256:deadbeef"),
    ]
    m = compute_metrics(events)
    assert m["tool_failure_count"] == 1


def test_lone_tool_blocked_is_informational_not_a_call():
    events = [_ev("tool_blocked", seq=0, tool="run_shell", reason="not_in_allowlist")]
    m = compute_metrics(events)
    assert m["tool_blocked_count"] == 1
    assert m["total_tool_calls"] == 0
    assert m["tool_failure_count"] == 0
    assert m["per_agent"]["main"]["tool_blocked_count"] == 1


def test_blocked_tool_triple_counts_once():
    events = [
        _ev("tool_call", seq=0, tool="run_shell", input={"command": "ls"}, tool_use_id="t0"),
        _ev("tool_blocked", seq=1, tool="run_shell", reason="not_in_allowlist"),
        _ev("tool_result", seq=2, tool="run_shell", tool_use_id="t0",
            result="Error: tool 'run_shell' is not permitted for this sub-agent."),
    ]
    m = compute_metrics(events)
    assert m["total_tool_calls"] == 1
    assert m["tool_failure_count"] == 1
    assert m["tool_blocked_count"] == 1
    assert m["per_tool"]["run_shell"]["calls"] == 1
    assert m["per_tool"]["run_shell"]["failures"] == 1


def test_permission_deny_rate():
    events = [
        _ev("permission_decision", seq=0, tool="run_shell", action="deny"),
        _ev("permission_decision", seq=1, tool="write_file", action="allow"),
        _ev("permission_decision", seq=2, tool="run_shell", action="deny"),
    ]
    m = compute_metrics(events)
    assert m["permission_decision_count"] == 3
    assert m["permission_deny_count"] == 2
    assert abs(m["deny_rate"] - (2 / 3)) < 1e-9


def test_compaction_count():
    events = [
        _ev("compaction", seq=0, kind="auto"),
        _ev("compaction", seq=1, kind="auto"),
    ]
    m = compute_metrics(events)
    assert m["compaction_count"] == 2


def test_model_latency_from_explicit_latency_ms():
    # B2：树 ts 秒级，model latency 取 llm_response 上显式 latency_ms（毫秒级真值）。
    events = [
        _ev("llm_request", seq=0),
        _ev("llm_response", seq=1, input_tokens=1, output_tokens=1, latency_ms=2000),
        _ev("llm_request", seq=2),
        _ev("llm_response", seq=3, input_tokens=1, output_tokens=1, latency_ms=1000),
    ]
    m = compute_metrics(events)
    assert m["model_latency_ms"]["sum"] == 3000
    assert m["model_latency_ms"]["count"] == 2
    assert m["model_latency_ms"]["avg"] == 1500.0


def test_model_latency_falls_back_to_ts_delta():
    # 无显式 latency_ms 时退回 ts 差（兼容旧 wire 派生流）。
    events = [
        _ev("llm_request", seq=0, ts="2026-06-09T00:00:00+00:00"),
        _ev("llm_response", seq=1, ts="2026-06-09T00:00:02+00:00", input_tokens=1, output_tokens=1),
    ]
    m = compute_metrics(events)
    assert m["model_latency_ms"]["sum"] == 2000
    assert m["model_latency_ms"]["count"] == 1


def test_tool_latency_from_explicit_latency_ms_by_tool():
    events = [
        _ev("tool_call", seq=0, tool="read_file", input={"file_path": "/a"}, tool_use_id="t0"),
        _ev("tool_result", seq=1, tool="read_file", tool_use_id="t0", result="ok", latency_ms=1000),
        _ev("tool_call", seq=2, tool="grep_search", input={}, tool_use_id="t1"),
        _ev("tool_result", seq=3, tool="grep_search", tool_use_id="t1", result="ok", latency_ms=3000),
    ]
    m = compute_metrics(events)
    assert m["tool_latency_ms"]["sum"] == 4000
    assert m["tool_latency_ms"]["count"] == 2
    assert m["per_tool"]["read_file"]["latency_ms_avg"] == 1000.0
    assert m["per_tool"]["grep_search"]["latency_ms_avg"] == 3000.0


def test_files_touched_dedup_and_sorted():
    events = [
        _ev("tool_call", seq=0, tool="read_file", input={"file_path": "/b.py"}, tool_use_id="a"),
        _ev("tool_call", seq=1, tool="write_file", input={"file_path": "/a.py"}, tool_use_id="b"),
        _ev("tool_call", seq=2, tool="edit_file", input={"file_path": "/a.py"}, tool_use_id="c"),
        _ev("tool_call", seq=3, tool="grep_search", input={"file_path": "/ignored"}, tool_use_id="d"),
    ]
    m = compute_metrics(events)
    assert m["files_touched"] == ["/a.py", "/b.py"]


def test_tests_run_pass_fail_heuristic():
    events = [
        _ev("tool_call", seq=0, tool="run_shell", input={"command": "pytest -q"}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="run_shell", tool_use_id="a",
            result="..F.\n3 passed, 1 failed in 0.42s"),
        _ev("tool_call", seq=2, tool="run_shell", input={"command": "echo hi"}, tool_use_id="b"),
        _ev("tool_result", seq=3, tool="run_shell", tool_use_id="b", result="hi"),
    ]
    m = compute_metrics(events)
    assert m["tests_run"] == 1
    assert m["tests_passed"] == 3
    assert m["tests_failed"] == 1


def test_timeout_cancel_error_best_effort():
    events = [
        _ev("budget_exceeded", seq=0, reason="max_cost"),
        _ev("session_end", seq=1, final_status="cancelled"),
        _ev("turn_end", seq=2),  # no signal
    ]
    m = compute_metrics(events)
    assert m["timeout_cancel_error_count"] == 2


def test_high_risk_from_steps_dataclass():
    steps = [
        Step("traj", "s1", "step_main_0", None, None, "main", "tool_action", risk_level="high"),
        Step("traj", "s1", "step_main_1", None, None, "main", "tool_action", risk_level="low"),
        Step("traj", "s1", "step_main_2", None, None, "main", "tool_action", risk_level="high"),
    ]
    m = compute_metrics([], steps=steps)
    assert m["high_risk_action_count"] == 2


def test_high_risk_from_steps_records():
    recs = [
        {"metadata": {"risk_level": "high"}},
        {"metadata": {"risk_level": "medium"}},
        {"risk_level": "high"},
    ]
    m = compute_metrics([], steps=recs)
    assert m["high_risk_action_count"] == 2


def test_per_agent_breakdown():
    events = [
        _ev("turn_end", agent_id="main", seq=0),
        _ev("tool_call", agent_id="main", seq=1, tool="read_file", input={"file_path": "/a"}, tool_use_id="a"),
        _ev("tool_result", agent_id="main", seq=2, tool="read_file", tool_use_id="a", result="ok"),
        _ev("tool_call", agent_id="agent-001", seq=0, tool="run_shell", input={}, tool_use_id="b"),
        _ev("tool_result", agent_id="agent-001", seq=1, tool="run_shell", tool_use_id="b", result="Error: x"),
        _ev("llm_response", agent_id="agent-001", seq=2, input_tokens=10, output_tokens=20),
    ]
    m = compute_metrics(events)
    assert set(m["per_agent"]) == {"main", "agent-001"}
    assert m["per_agent"]["main"]["total_turns"] == 1
    assert m["per_agent"]["main"]["total_tool_calls"] == 1
    assert m["per_agent"]["main"]["tool_failure_count"] == 0
    assert m["per_agent"]["agent-001"]["total_tool_calls"] == 1
    assert m["per_agent"]["agent-001"]["tool_failure_count"] == 1
    assert m["per_agent"]["agent-001"]["input_tokens"] == 10
    assert m["per_agent"]["agent-001"]["output_tokens"] == 20


def test_per_tool_breakdown():
    events = [
        _ev("tool_call", seq=0, tool="read_file", input={"file_path": "/a"}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="read_file", tool_use_id="a", result="ok"),
        _ev("tool_call", seq=2, tool="read_file", input={"file_path": "/b"}, tool_use_id="b"),
        _ev("tool_result", seq=3, tool="read_file", tool_use_id="b", result="Error: nope"),
    ]
    m = compute_metrics(events)
    assert m["per_tool"]["read_file"]["calls"] == 2
    assert m["per_tool"]["read_file"]["failures"] == 1


def test_malformed_missing_fields_do_not_crash():
    events = [
        _ev("tool_call", seq=0),
        _ev("tool_result", seq=1),
        _ev("llm_response", seq=2),
        _ev("permission_decision", seq=3),
        TrajEvent(type="tool_call", agent_id="main", seq=4, ts="", session_id="s1"),
    ]
    m = compute_metrics(events)
    assert m["total_tool_calls"] == 2
    assert m["total_input_tokens"] == 0
    assert m["permission_deny_count"] == 0
    assert "<unknown>" in m["per_tool"]


def test_negative_latency_clamped_to_zero():
    # 显式 latency_ms 为负（异常上游）→ 钳为 0，不出负数。
    events = [
        _ev("tool_call", seq=0, tool="x", input={}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="x", tool_use_id="a", result="ok", latency_ms=-5),
    ]
    m = compute_metrics(events)
    assert m["tool_latency_ms"]["sum"] == 0
    assert m["tool_latency_ms"]["count"] == 1
