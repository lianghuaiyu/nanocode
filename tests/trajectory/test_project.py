"""trajectory.project 的单测：wire 事件 -> Step 投影（docs/10 P2 验收）。

覆盖：clean tool_action、llm_decision、summary-mode tool_result（只有 result_summary/hash）、
legacy 行、malformed/缺字段行——断言稳定 step 输出且**绝不**崩溃。

构造方式：直接用 ``SessionEvent.from_wire`` 把 flat wire dict 解析为 SessionEvent
（与生产读侧 reader 同路径），不依赖任何 runtime 模块。
"""
from __future__ import annotations

from nanocode.events.models import SessionEvent
from nanocode.trajectory import schema
from nanocode.trajectory.project import build_steps


def _ev(d: dict, *, agent_id: str = "main") -> SessionEvent:
    """把一行 flat wire dict 解析为 SessionEvent（注入 agent_id，复刻 reader 行为）。"""
    ev = SessionEvent.from_wire(d, agent_id=agent_id)
    ev.line_no = d.get("seq", 0)
    return ev


def _wire(seq: int, etype: str, *, ts: str = "", agent: str = "main", **payload) -> dict:
    """合成一行新式 wire dict（带 envelope id），payload 顶层扁平。"""
    return {
        "v": 1,
        "id": f"evt_{agent}_{seq}",
        "session_id": "sess_test",
        "agent_id": agent,
        "branch_id": "main",
        "seq": seq,
        "ts": ts or f"2026-06-09T10:00:{seq:02d}+00:00",
        "turn_id": "turn_main_0",
        "type": etype,
        **payload,
    }


# ── clean tool_action ─────────────────────────────────────────


def test_clean_tool_action_pairs_call_and_result():
    events = [
        _ev(_wire(0, "assistant_message", text="I will read the file",
                  tool_uses=[{"id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}])),
        _ev(_wire(1, "tool_call", tool="read_file", tool_use_id="tu_1",
                  input={"path": "a.py"}, ts="2026-06-09T10:00:01+00:00")),
        _ev(_wire(2, "tool_result", tool="read_file", tool_use_id="tu_1",
                  chars=12, result="file contents", ts="2026-06-09T10:00:01.850000+00:00")),
    ]
    steps = build_steps(events)
    tool_steps = [s for s in steps if s.step_type == "tool_action"]
    assert len(tool_steps) == 1
    s = tool_steps[0]
    assert s.action == {"type": "tool_call", "tool": "read_file",
                        "args_summary": '{"path": "a.py"}'}
    assert s.result_summary == "file contents"
    assert s.observation_summary == "I will read the file"
    assert s.step_id == schema.step_id("main", 1)
    assert s.latency_ms == 850
    assert s.risk_level == "low"
    assert s.eval_result is None and s.reward is None
    assert s.done is False
    assert s.trajectory_id == "traj_sess_test"
    assert s.episode_id == "sess_test"


def test_tool_action_without_result_has_none_latency():
    events = [_ev(_wire(0, "tool_call", tool="read_file", tool_use_id="tu_x",
                        input={"path": "x"}))]
    steps = build_steps(events)
    assert len(steps) == 1
    assert steps[0].step_type == "tool_action"
    assert steps[0].latency_ms is None
    assert steps[0].result_summary == ""


# ── llm_decision ──────────────────────────────────────────────


def test_llm_decision_request_assistant_response():
    events = [
        _ev(_wire(0, "llm_request", model="claude", message_count=3,
                  messages=[{"role": "user", "content": "hi"}],
                  ts="2026-06-09T10:00:00+00:00")),
        _ev(_wire(1, "assistant_message", text="working on it",
                  tool_uses=[{"id": "tu_1", "name": "read_file", "input": {}}])),
        _ev(_wire(2, "llm_response", input_tokens=1000, output_tokens=200,
                  ts="2026-06-09T10:00:00.500000+00:00")),
    ]
    steps = build_steps(events)
    dec = [s for s in steps if s.step_type == "llm_decision"]
    assert len(dec) == 1
    s = dec[0]
    assert s.input_tokens == 1000
    assert s.output_tokens == 200
    assert s.latency_ms == 500
    assert s.action["type"] == "assistant"
    assert s.action["n_tool_uses"] == 1
    assert "messages=3" in s.observation_summary
    assert s.step_id == schema.step_id("main", 0)  # 锚在 llm_request seq
    assert s.done is False


def test_llm_decision_empty_tool_uses_produces_final():
    events = [
        _ev(_wire(0, "llm_request", model="claude", message_count=2,
                  messages=[{"role": "user", "content": "done?"}])),
        _ev(_wire(1, "assistant_message", text="All done.", tool_uses=[])),
        _ev(_wire(2, "llm_response", input_tokens=10, output_tokens=5)),
    ]
    steps = build_steps(events)
    finals = [s for s in steps if s.step_type == "final"]
    assert len(finals) == 1
    # 回合级最终答复 done=False（episode=session，仅 session_end 才 done=True）。
    assert finals[0].done is False
    assert finals[0].result_summary == "All done."


# ── summary-mode tool_result（只有 result_summary / hash）──────


def test_summary_mode_tool_result_uses_summary_fields():
    # SUMMARY 级：apply_summary_shaping 已 pop result，留 result_summary + result_hash + chars
    events = [
        _ev(_wire(0, "tool_call", tool="run_shell", tool_use_id="tu_9",
                  input={"command": "ls"}, ts="2026-06-09T10:00:00+00:00")),
        _ev(_wire(1, "tool_result", tool="run_shell", tool_use_id="tu_9", chars=42,
                  result_summary="file1\nfile2", result_hash="sha256:abc",
                  ts="2026-06-09T10:00:00.100000+00:00")),
    ]
    steps = build_steps(events)
    assert len(steps) == 1
    s = steps[0]
    assert s.result_summary == "file1\nfile2"
    assert s.risk_level == "medium"  # run_shell 写/执行类
    assert s.latency_ms == 100


def test_summary_mode_tool_call_only_hash_emits_summarized_placeholder():
    # 假设 summary 级把 input 也整成 hash（防御性分支）
    events = [
        _ev(_wire(0, "tool_call", tool="write_file", tool_use_id="tu_h",
                  input_hash="sha256:deadbeef")),
        _ev(_wire(1, "tool_result", tool="write_file", tool_use_id="tu_h",
                  result_hash="sha256:cafe")),
    ]
    steps = build_steps(events)
    assert steps[0].action["args_summary"] == "(summarized)"
    assert steps[0].result_summary == "(summarized)"
    assert steps[0].risk_level == "medium"


# ── 风险启发 ──────────────────────────────────────────────────


def test_dangerous_shell_command_is_high_risk():
    events = [
        _ev(_wire(0, "tool_call", tool="run_shell", tool_use_id="tu_rm",
                  input={"command": "rm -rf /tmp/x"})),
        _ev(_wire(1, "tool_result", tool="run_shell", tool_use_id="tu_rm", result="ok")),
    ]
    steps = build_steps(events)
    assert steps[0].risk_level == "high"


def test_permission_deny_makes_tool_high_risk():
    events = [
        _ev(_wire(0, "permission_decision", tool="write_file", action="deny",
                  message="not allowed")),
        _ev(_wire(1, "tool_call", tool="write_file", tool_use_id="tu_d",
                  input={"path": "secret"})),
        _ev(_wire(2, "tool_result", tool="write_file", tool_use_id="tu_d", result="denied")),
    ]
    steps = build_steps(events)
    tool = [s for s in steps if s.step_type == "tool_action"][0]
    assert tool.risk_level == "high"


# ── final from terminal events ────────────────────────────────


def test_turn_end_and_session_end_produce_final_steps():
    events = [
        _ev(_wire(0, "turn_end", input_tokens=100, output_tokens=50, turns=2)),
        _ev(_wire(1, "session_end", input_tokens=100, output_tokens=50, turns=2)),
    ]
    steps = build_steps(events)
    assert len(steps) == 2
    assert all(s.step_type == "final" for s in steps)
    # episode=session：仅 session_end 终止（done=True）；turn_end 是回合边界（done=False）。
    assert steps[0].action == {"type": "turn_end"} and steps[0].done is False
    assert steps[1].action == {"type": "session_end"} and steps[1].done is True
    # 终止标记不带累计 token（累计只在 metrics/metadata；避免 per-step 成本重复摊算）。
    assert steps[0].input_tokens == 0 and steps[0].output_tokens == 0
    assert steps[1].input_tokens == 0 and steps[1].output_tokens == 0


# ── parent_step_id chain ──────────────────────────────────────


def test_parent_step_id_chains_within_agent():
    events = [
        _ev(_wire(0, "tool_call", tool="read_file", tool_use_id="t0", input={})),
        _ev(_wire(1, "tool_result", tool="read_file", tool_use_id="t0", result="r0")),
        _ev(_wire(2, "tool_call", tool="read_file", tool_use_id="t1", input={})),
        _ev(_wire(3, "tool_result", tool="read_file", tool_use_id="t1", result="r1")),
    ]
    steps = build_steps(events)
    assert steps[0].parent_step_id is None
    assert steps[1].parent_step_id == steps[0].step_id


def test_parent_chains_are_per_agent():
    events = [
        _ev(_wire(0, "tool_call", tool="read_file", tool_use_id="m0", input={}, agent="main")),
        _ev(_wire(0, "tool_call", tool="read_file", tool_use_id="s0", input={},
                  agent="agent-001"), agent_id="agent-001"),
    ]
    steps = build_steps(events)
    # 两个不同 agent 的首个 step 都应 parent=None（链按 agent 隔离）
    assert all(s.parent_step_id is None for s in steps)
    ids = {s.agent_id for s in steps}
    assert ids == {"main", "agent-001"}


def test_fork_branch_lineage_not_flattened():
    """fork 分支不被压扁、且保留 branch 身份（审阅 HIGH）。

    main 分支跑一个 tool_action（evt_main_0），随后从该事件 fork 出分支 b1（其首事件带
    parent_event_id=evt_main_0）。断言：b1 的 step 带 branch_id='b1'、parent 指向 fork 点的
    step_main_0（而非被串进 main 链或丢失分支）；main step 仍 branch_id='main'、parent=None。
    """
    events = [
        _ev(_wire(0, "tool_call", tool="read_file", tool_use_id="m0", input={"path": "a"})),
        _ev(_wire(1, "tool_result", tool="read_file", tool_use_id="m0", result="ra")),
        # fork：同 agent(main)、新 branch b1，首事件带 parent_event_id 指向 fork 点 evt_main_0。
        _ev(_wire(2, "tool_call", tool="run_shell", tool_use_id="b0", input={"command": "ls"},
                  branch_id="b1", parent_event_id="evt_main_0")),
        _ev(_wire(3, "tool_result", tool="run_shell", tool_use_id="b0", result="rb",
                  branch_id="b1")),
    ]
    steps = build_steps(events)
    by_id = {s.step_id: s for s in steps}
    main_step = by_id["step_main_0"]
    fork_step = by_id["step_main_2"]
    assert main_step.branch_id == "main" and main_step.parent_step_id is None
    assert fork_step.branch_id == "b1"
    # 分支首 step 接到 fork 点的 step（解析自 parent_event_id=evt_main_0），不被压进 main 链。
    assert fork_step.parent_step_id == "step_main_0"
    # branch_id 也进 to_record 的 metadata。
    assert fork_step.to_record()["metadata"]["branch_id"] == "b1"


# ── legacy 行 ─────────────────────────────────────────────────


def test_legacy_row_does_not_crash_and_projects():
    # 升级前 wire：无 envelope id/branch_id，payload 在顶层
    legacy_call = _ev({"v": 1, "ts": "2026-06-08T10:00:00Z", "session_id": "sess_test",
                       "seq": 0, "type": "tool_call", "tool": "grep_search",
                       "input": {"pattern": "E"}, "tool_use_id": "lu_1"})
    legacy_res = _ev({"v": 1, "ts": "2026-06-08T10:00:01Z", "session_id": "sess_test",
                      "seq": 1, "type": "tool_result", "tool": "grep_search",
                      "tool_use_id": "lu_1", "result": "match"})
    assert legacy_call.legacy is True
    steps = build_steps([legacy_call, legacy_res])
    assert len(steps) == 1
    assert steps[0].step_type == "tool_action"
    assert steps[0].action["tool"] == "grep_search"
    assert steps[0].result_summary == "match"
    # Z 后缀 ts 也能解析 latency（1s）
    assert steps[0].latency_ms == 1000


# ── malformed / missing-field 行 ──────────────────────────────


def test_malformed_and_missing_fields_never_crash():
    events = [
        _ev({"type": "tool_call"}),  # 无 seq/tool/tool_use_id/input
        _ev({"type": "tool_result"}),  # 无任何配对键
        _ev({"type": "assistant_message"}),  # 无 text / tool_uses
        _ev({"type": "llm_response"}),  # 无 token
        _ev({"type": "unknown_event_type", "foo": "bar"}),  # 未知类型
        _ev({}),  # 空 dict -> type=""
    ]
    # 绝不抛
    steps = build_steps(events)
    # 一个无 tool_use_id 的 tool_call 仍投影为 tool_action（无 result）
    tool_steps = [s for s in steps if s.step_type == "tool_action"]
    assert len(tool_steps) == 1
    assert tool_steps[0].result_summary == ""
    assert tool_steps[0].action["tool"] == ""
    # assistant_message 无 tool_uses -> llm_decision + final
    types = [s.step_type for s in steps]
    assert "llm_decision" in types
    assert "final" in types


def test_build_steps_empty_input():
    assert build_steps([]) == []


def test_eval_and_reward_always_none_in_projection():
    events = [
        _ev(_wire(0, "tool_call", tool="read_file", tool_use_id="e0", input={})),
        _ev(_wire(1, "tool_result", tool="read_file", tool_use_id="e0", result="r")),
        _ev(_wire(2, "turn_end", input_tokens=1, output_tokens=1, turns=1)),
    ]
    steps = build_steps(events)
    assert steps  # non-empty
    for s in steps:
        assert s.eval_result is None
        assert s.reward is None


def test_to_record_shape_round_trips():
    events = [
        _ev(_wire(0, "tool_call", tool="edit_file", tool_use_id="r0",
                  input={"path": "z"}, ts="2026-06-09T10:00:00+00:00")),
        _ev(_wire(1, "tool_result", tool="edit_file", tool_use_id="r0",
                  result="done", ts="2026-06-09T10:00:00.250000+00:00")),
    ]
    rec = build_steps(events)[0].to_record()
    assert rec["step_type"] == "tool_action"
    assert rec["action"]["tool"] == "edit_file"
    assert rec["cost"]["latency_ms"] == 250
    assert rec["metadata"]["risk_level"] == "medium"
    assert rec["metadata"]["agent_id"] == "main"
    assert rec["reward"] is None
    assert rec["eval_result"] is None
