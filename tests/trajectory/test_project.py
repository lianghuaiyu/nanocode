"""trajectory.project 的单测：canonical 树派生事件 -> Step 投影（docs/14 Milestone B2）。

覆盖：clean tool_action、llm_decision、summary-mode 字段、malformed/缺字段、final from
terminal、parent chain、多 agent 隔离——断言稳定 step 输出且**绝不**崩溃。

构造方式：用真实 ``SessionManager`` 树（tests.trajectory._fixtures）+ ``tree_events`` 适配器
重建事件流，复刻生产读侧路径，不依赖任何 runtime 模块。直接 ``build_steps`` 单测则用
``_te`` 合成 TrajEvent（旧 wire dict 的等价物）。
"""
from __future__ import annotations

from nanocode.trajectory import schema
from nanocode.trajectory._tree_events import TrajEvent, tree_events
from nanocode.trajectory.project import build_steps, project_session

from tests.trajectory import _fixtures as F


def _te(seq: int, etype: str, *, agent: str = "main", ts: str = "",
        parent_event_id: "str | None" = None, branch_id: str = "main", **data) -> TrajEvent:
    """合成一条 TrajEvent（payload 落 data，等价于旧 wire 行的解析结果）。"""
    return TrajEvent(
        type=etype, agent_id=agent, seq=seq,
        ts=ts or f"2026-06-09T10:00:{seq:02d}+00:00",
        session_id="sess_test", id=f"evt_{agent}_{seq}",
        parent_id=None, parent_event_id=parent_event_id, branch_id=branch_id,
        turn_id="turn_main_0", line_no=seq, data=dict(data),
    )


# ── clean tool_action（端到端 tree → steps）────────────────────


def test_clean_tool_action_pairs_call_and_result():
    m = F.new_session("s_clean")
    F.append_user(m, "read it")
    F.append_assistant(m, text="I will read the file",
                       tool_calls=[{"id": "tu_1", "name": "read_file",
                                    "arguments": {"path": "a.py"}}],
                       input_tokens=100, output_tokens=20, latency_ms=300)
    F.append_tool_result(m, tool_call_id="tu_1", tool_name="read_file",
                         content="file contents", latency_ms=850)
    m.close()

    steps = project_session("s_clean")
    tool_steps = [s for s in steps if s.step_type == "tool_action"]
    assert len(tool_steps) == 1
    s = tool_steps[0]
    assert s.action == {"type": "tool_call", "tool": "read_file",
                        "args_summary": '{"path": "a.py"}'}
    assert s.result_summary == "file contents"
    assert s.observation_summary == "I will read the file"
    assert s.latency_ms == 850          # tool_result 的显式 latency_ms（毫秒级真值）
    assert s.risk_level == "low"
    assert s.eval_result is None and s.reward is None
    assert s.done is False
    assert s.trajectory_id == "traj_s_clean"
    assert s.episode_id == "s_clean"


def test_tool_action_without_result_has_none_latency():
    events = [_te(0, "tool_call", tool="read_file", tool_use_id="tu_x", input={"path": "x"})]
    steps = build_steps(events)
    assert len(steps) == 1
    assert steps[0].step_type == "tool_action"
    assert steps[0].latency_ms is None
    assert steps[0].result_summary == ""


# ── llm_decision ──────────────────────────────────────────────


def test_llm_decision_request_assistant_response():
    events = [
        _te(0, "llm_request", model="claude", message_count=3, messages_chars=120),
        _te(1, "assistant_message", text="working on it",
            tool_uses=[{"id": "tu_1", "name": "read_file", "input": {}}]),
        _te(2, "llm_response", input_tokens=1000, output_tokens=200, latency_ms=500),
    ]
    steps = build_steps(events)
    dec = [s for s in steps if s.step_type == "llm_decision"]
    assert len(dec) == 1
    s = dec[0]
    assert s.input_tokens == 1000
    assert s.output_tokens == 200
    assert s.latency_ms == 500          # 来自 llm_response 显式 latency_ms
    assert s.action["type"] == "assistant"
    assert s.action["n_tool_uses"] == 1
    assert "messages=3" in s.observation_summary
    assert s.step_id == schema.step_id("main", 0)  # 锚在 llm_request seq
    assert s.done is False


def test_llm_decision_empty_tool_uses_produces_final():
    events = [
        _te(0, "llm_request", model="claude", message_count=2, messages_chars=20),
        _te(1, "assistant_message", text="All done.", tool_uses=[]),
        _te(2, "llm_response", input_tokens=10, output_tokens=5, latency_ms=42),
    ]
    steps = build_steps(events)
    finals = [s for s in steps if s.step_type == "final"]
    assert len(finals) == 1
    assert finals[0].done is False
    assert finals[0].result_summary == "All done."


# ── summary-mode tool_result（只有 result_summary / hash）──────


def test_summary_mode_tool_result_uses_summary_fields():
    events = [
        _te(0, "tool_call", tool="run_shell", tool_use_id="tu_9", input={"command": "ls"}),
        _te(1, "tool_result", tool="run_shell", tool_use_id="tu_9",
            result_summary="file1\nfile2", result_hash="sha256:abc", latency_ms=100),
    ]
    steps = build_steps(events)
    assert len(steps) == 1
    s = steps[0]
    assert s.result_summary == "file1\nfile2"
    assert s.risk_level == "medium"  # run_shell 写/执行类
    assert s.latency_ms == 100


def test_summary_mode_tool_call_only_hash_emits_summarized_placeholder():
    events = [
        _te(0, "tool_call", tool="write_file", tool_use_id="tu_h", input_hash="sha256:deadbeef"),
        _te(1, "tool_result", tool="write_file", tool_use_id="tu_h", result_hash="sha256:cafe"),
    ]
    steps = build_steps(events)
    assert steps[0].action["args_summary"] == "(summarized)"
    assert steps[0].result_summary == "(summarized)"
    assert steps[0].risk_level == "medium"


# ── 风险启发 ──────────────────────────────────────────────────


def test_dangerous_shell_command_is_high_risk():
    events = [
        _te(0, "tool_call", tool="run_shell", tool_use_id="tu_rm",
            input={"command": "rm -rf /tmp/x"}),
        _te(1, "tool_result", tool="run_shell", tool_use_id="tu_rm", result="ok"),
    ]
    steps = build_steps(events)
    assert steps[0].risk_level == "high"


def test_permission_deny_makes_tool_high_risk():
    events = [
        _te(0, "permission_decision", tool="write_file", action="deny", message="not allowed"),
        _te(1, "tool_call", tool="write_file", tool_use_id="tu_d", input={"path": "secret"}),
        _te(2, "tool_result", tool="write_file", tool_use_id="tu_d", result="denied"),
    ]
    steps = build_steps(events)
    tool = [s for s in steps if s.step_type == "tool_action"][0]
    assert tool.risk_level == "high"


# ── final from terminal events ────────────────────────────────


def test_turn_end_and_session_end_produce_final_steps():
    events = [
        _te(0, "turn_end", input_tokens=100, output_tokens=50, turns=2),
        _te(1, "session_end", final_status="completed"),
    ]
    steps = build_steps(events)
    assert len(steps) == 2
    assert all(s.step_type == "final" for s in steps)
    assert steps[0].action == {"type": "turn_end"} and steps[0].done is False
    assert steps[1].action == {"type": "session_end"} and steps[1].done is True
    assert steps[0].input_tokens == 0 and steps[0].output_tokens == 0
    assert steps[1].input_tokens == 0 and steps[1].output_tokens == 0


# ── parent_step_id chain ──────────────────────────────────────


def test_parent_step_id_chains_within_agent():
    events = [
        _te(0, "tool_call", tool="read_file", tool_use_id="t0", input={}),
        _te(1, "tool_result", tool="read_file", tool_use_id="t0", result="r0"),
        _te(2, "tool_call", tool="read_file", tool_use_id="t1", input={}),
        _te(3, "tool_result", tool="read_file", tool_use_id="t1", result="r1"),
    ]
    steps = build_steps(events)
    assert steps[0].parent_step_id is None
    assert steps[1].parent_step_id == steps[0].step_id


def test_parent_chains_are_per_agent():
    events = [
        _te(0, "tool_call", tool="read_file", tool_use_id="m0", input={}, agent="main"),
        _te(0, "tool_call", tool="read_file", tool_use_id="s0", input={}, agent="agent-001"),
    ]
    steps = build_steps(events)
    assert all(s.parent_step_id is None for s in steps)
    ids = {s.agent_id for s in steps}
    assert ids == {"main", "agent-001"}


def test_multi_agent_via_child_session():
    """端到端：父会话 + 子会话（parentSession.agentId）→ tree_events fan-out 两个 agent。"""
    p = F.new_session("ma_parent")
    F.append_user(p, "spawn")
    F.append_assistant(p, text="spawning",
                       tool_calls=[{"id": "sub", "name": "task", "arguments": {}}],
                       input_tokens=5, output_tokens=2, latency_ms=100)
    F.append_tool_result(p, tool_call_id="sub", tool_name="task", content="ok", latency_ms=50)
    c = F.child_session(p, "ma_child", agent_id="agent-001")
    F.append_user(c, "sub task")
    F.append_assistant(c, text="running",
                       tool_calls=[{"id": "cs", "name": "read_file", "arguments": {}}],
                       input_tokens=8, output_tokens=3, latency_ms=120)
    F.append_tool_result(c, tool_call_id="cs", tool_name="read_file", content="ok", latency_ms=70)
    p.close()
    c.close()

    steps = build_steps(tree_events("ma_parent"))
    agents = {s.agent_id for s in steps}
    assert "main" in agents
    assert "agent-001" in agents
    # 各 agent 链内 parent 隔离：每个 agent 的首 step parent=None。
    for aid in agents:
        first = next(s for s in steps if s.agent_id == aid)
        assert first.parent_step_id is None


# ── malformed / missing-field ─────────────────────────────────


def test_malformed_and_missing_fields_never_crash():
    events = [
        _te(0, "tool_call"),
        _te(1, "tool_result"),
        _te(2, "assistant_message"),
        _te(3, "llm_response"),
        _te(4, "unknown_event_type", foo="bar"),
        TrajEvent(type="", agent_id="main", seq=5, ts="", session_id="sess_test"),
    ]
    steps = build_steps(events)
    tool_steps = [s for s in steps if s.step_type == "tool_action"]
    assert len(tool_steps) == 1
    assert tool_steps[0].result_summary == ""
    assert tool_steps[0].action["tool"] == ""
    types = [s.step_type for s in steps]
    assert "llm_decision" in types
    assert "final" in types


def test_build_steps_empty_input():
    assert build_steps([]) == []


def test_project_missing_session_returns_empty():
    assert project_session("never-existed") == []


def test_eval_and_reward_always_none_in_projection():
    events = [
        _te(0, "tool_call", tool="read_file", tool_use_id="e0", input={}),
        _te(1, "tool_result", tool="read_file", tool_use_id="e0", result="r"),
        _te(2, "turn_end", input_tokens=1, output_tokens=1, turns=1),
    ]
    steps = build_steps(events)
    assert steps
    for s in steps:
        assert s.eval_result is None
        assert s.reward is None


def test_to_record_shape_round_trips():
    events = [
        _te(0, "tool_call", tool="edit_file", tool_use_id="r0", input={"path": "z"}),
        _te(1, "tool_result", tool="edit_file", tool_use_id="r0", result="done", latency_ms=250),
    ]
    rec = build_steps(events)[0].to_record()
    assert rec["step_type"] == "tool_action"
    assert rec["action"]["tool"] == "edit_file"
    assert rec["cost"]["latency_ms"] == 250
    assert rec["metadata"]["risk_level"] == "medium"
    assert rec["metadata"]["agent_id"] == "main"
    assert rec["reward"] is None
    assert rec["eval_result"] is None


def test_tree_events_forked_branches_get_distinct_ids_and_fork_point():
    # review medium 回归：in-file fork（同 conv parent 的第二个 conv 子）→ 独立 branch_id + fork-point
    # parent_event_id，不再全部线性混入 "main"（取代硬编码 branch_id="main"）。
    from nanocode.trajectory._tree_events import tree_events
    from nanocode.session.manager import SessionManager
    from nanocode.session import tree as T
    m = SessionManager.create("forktraj")
    u1 = m.append_message(T.user_message("q1"))
    m.append_message(T.assistant_message([T.text_block("a1")], provider="anthropic",
                     api="anthropic", model="cx", stop_reason="stop"))   # u1 的首子 → main
    # fork：u1 的第二个 conv 子（divergent branch）
    u2 = m.append(T.MESSAGE, {"message": T.user_message("q1-redo")}, parent_id=u1.id)
    m.append_message(T.assistant_message([T.text_block("a2")], provider="anthropic",
                     api="anthropic", model="cx", stop_reason="stop"))   # u2 的子 → b1
    evs = tree_events("forktraj")
    branches = {e.branch_id for e in evs}
    assert "main" in branches and "b1" in branches            # 两条 branch，不再线性
    # b1 首个 emitted event 链到 fork point u1（user 消息不产 event，故挂在 a2 的 assistant_message 上）
    b1_evs = [e for e in evs if e.branch_id == "b1"]
    assert b1_evs and b1_evs[0].parent_event_id == u1.id
