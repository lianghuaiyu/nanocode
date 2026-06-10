"""tests for trajectory.metrics.compute_metrics（P3 harness 指标聚合）。

构造 SessionEvent 列表（经 from_wire，贴近真实 wire 行），断言每个指标的计数/比率，
并覆盖 legacy / summary / malformed 容忍与 per_agent / per_tool breakdown。
"""
from __future__ import annotations

from nanocode.events.models import SessionEvent
from nanocode.trajectory.metrics import compute_metrics
from nanocode.trajectory.schema import Step


def _ev(etype, *, agent_id="main", seq=0, ts=None, **payload):
    """构造一条 SessionEvent（经 from_wire），payload 落到 .data。"""
    if ts is None:
        ts = f"2026-06-09T00:00:{seq:02d}+00:00"
    d = {"type": etype, "seq": seq, "ts": ts, "session_id": "s1", "agent_id": agent_id, **payload}
    # 带 id 即非 legacy；from_wire 会据 ENVELOPE_KEYS 归集 payload 到 .data。
    d["id"] = f"evt_{agent_id}_{seq}"
    return SessionEvent.from_wire(d, agent_id=agent_id)


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
    # 1M in * $3/M + 2M out * $15/M = 3 + 30 = 33
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
    assert m["tool_failure_count"] == 2  # Error + Warning
    assert abs(m["tool_failure_rate"] - (2 / 3)) < 1e-9


def test_failure_prefix_is_case_insensitive_and_leading_ws():
    events = [
        _ev("tool_call", seq=0, tool="x", input={}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="x", tool_use_id="a", result="  ERROR: nope"),
    ]
    m = compute_metrics(events)
    assert m["tool_failure_count"] == 1


def test_summary_level_result_uses_result_summary():
    # SUMMARY 级别：无 full result，只有 result_summary（apply_summary_shaping 形态）。
    events = [
        _ev("tool_call", seq=0, tool="run_shell", input={"command": "x"}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="run_shell", tool_use_id="a",
            result_summary="Error: failed", result_hash="sha256:deadbeef", chars=13),
    ]
    m = compute_metrics(events)
    assert m["tool_failure_count"] == 1


def test_lone_tool_blocked_is_informational_not_a_call():
    # 孤立 tool_blocked（无配对 tool_call/result，如 hook 拦截路径）：只记 tool_blocked_count，
    # 不计入 total_tool_calls / tool_failure_count（生产里 call 由 tool_call 计、failure 由
    # "Error: ... not permitted" 的 tool_result 计）。修审阅 HIGH 双计。
    events = [
        _ev("tool_blocked", seq=0, tool="run_shell", reason="not_in_allowlist"),
    ]
    m = compute_metrics(events)
    assert m["tool_blocked_count"] == 1
    assert m["total_tool_calls"] == 0
    assert m["tool_failure_count"] == 0
    assert m["per_agent"]["main"]["tool_blocked_count"] == 1


def test_blocked_tool_triple_counts_once():
    # 生产真实序列：tool_call -> tool_blocked -> tool_result("Error: tool ... not permitted")。
    # 一次被挡调用必须只记 1 call + 1 failure + 1 blocked（修审阅 HIGH：曾记 2 call + 2 failure）。
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


def test_model_latency_pairing():
    events = [
        _ev("llm_request", seq=0, ts="2026-06-09T00:00:00+00:00"),
        _ev("llm_response", seq=1, ts="2026-06-09T00:00:02+00:00", input_tokens=1, output_tokens=1),
        _ev("llm_request", seq=2, ts="2026-06-09T00:00:05+00:00"),
        _ev("llm_response", seq=3, ts="2026-06-09T00:00:06+00:00", input_tokens=1, output_tokens=1),
    ]
    m = compute_metrics(events)
    # 2000ms + 1000ms = 3000ms; avg 1500
    assert m["model_latency_ms"]["sum"] == 3000
    assert m["model_latency_ms"]["count"] == 2
    assert m["model_latency_ms"]["avg"] == 1500.0


def test_tool_latency_pairing_by_tool_use_id():
    events = [
        _ev("tool_call", seq=0, tool="read_file", input={"file_path": "/a"}, tool_use_id="t0",
            ts="2026-06-09T00:00:00+00:00"),
        _ev("tool_call", seq=1, tool="grep_search", input={}, tool_use_id="t1",
            ts="2026-06-09T00:00:00+00:00"),
        # result 顺序与 call 顺序不同——靠 tool_use_id 正确配对。
        _ev("tool_result", seq=2, tool="grep_search", tool_use_id="t1", result="ok",
            ts="2026-06-09T00:00:03+00:00"),
        _ev("tool_result", seq=3, tool="read_file", tool_use_id="t0", result="ok",
            ts="2026-06-09T00:00:01+00:00"),
    ]
    m = compute_metrics(events)
    # grep_search: 3000ms; read_file: 1000ms
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
    assert m["files_touched"] == ["/a.py", "/b.py"]  # deduped + sorted, grep ignored


def test_tests_run_pass_fail_heuristic():
    events = [
        _ev("tool_call", seq=0, tool="run_shell", input={"command": "pytest -q"}, tool_use_id="a"),
        _ev("tool_result", seq=1, tool="run_shell", tool_use_id="a",
            result="..F.\n3 passed, 1 failed in 0.42s"),
        _ev("tool_call", seq=2, tool="run_shell", input={"command": "echo hi"}, tool_use_id="b"),
        _ev("tool_result", seq=3, tool="run_shell", tool_use_id="b", result="hi"),
    ]
    m = compute_metrics(events)
    assert m["tests_run"] == 1  # only the pytest one
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
        {"risk_level": "high"},  # flat fallback
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


def test_legacy_rows_do_not_crash():
    # legacy 行：无 id（from_wire 会反推），缺 turn_id 等；应正常计入而不抛。
    legacy = SessionEvent.from_wire(
        {"type": "tool_call", "seq": 0, "ts": "2026-06-09T00:00:00+00:00",
         "tool": "read_file", "input": {"file_path": "/x"}, "tool_use_id": "t"},
        agent_id="main",
    )
    assert legacy.legacy is True
    result = SessionEvent.from_wire(
        {"type": "tool_result", "seq": 1, "ts": "2026-06-09T00:00:01+00:00",
         "tool": "read_file", "tool_use_id": "t", "result": "ok"},
        agent_id="main",
    )
    m = compute_metrics([legacy, result])
    assert m["total_tool_calls"] == 1
    assert m["files_touched"] == ["/x"]
    assert m["tool_latency_ms"]["count"] == 1


def test_malformed_missing_fields_do_not_crash():
    # 缺 tool / input / ts / tokens 的事件不应崩。
    events = [
        _ev("tool_call", seq=0),  # no tool, no input
        _ev("tool_result", seq=1),  # no tool, no result
        _ev("llm_response", seq=2),  # no tokens
        _ev("permission_decision", seq=3),  # no action
        SessionEvent.from_wire({"type": "tool_call", "id": "evt_main_4"}, agent_id="main"),  # no seq/ts
    ]
    m = compute_metrics(events)
    assert m["total_tool_calls"] == 2  # two tool_call events
    assert m["total_input_tokens"] == 0
    assert m["permission_deny_count"] == 0
    # <unknown> bucket created for tool-less calls
    assert "<unknown>" in m["per_tool"]


def test_negative_latency_clamped_to_zero():
    # result ts 早于 call ts（时钟回拨等）→ 钳为 0，不出负数。
    events = [
        _ev("tool_call", seq=0, tool="x", input={}, tool_use_id="a", ts="2026-06-09T00:00:05+00:00"),
        _ev("tool_result", seq=1, tool="x", tool_use_id="a", result="ok", ts="2026-06-09T00:00:00+00:00"),
    ]
    m = compute_metrics(events)
    assert m["tool_latency_ms"]["sum"] == 0
    assert m["tool_latency_ms"]["count"] == 1
