"""trajectory.schema：Step.to_record() 形态 + step_id helper + TrajectoryMetadata。"""
from nanocode.trajectory.schema import (
    Step,
    STEP_TYPES,
    TrajectoryMetadata,
    step_id,
)


def test_step_id_helper():
    assert step_id("main", 42) == "step_main_42"
    assert step_id("agent-001", 0) == "step_agent-001_0"


def test_step_types_contract():
    assert STEP_TYPES == frozenset(
        {"llm_decision", "tool_action", "approval", "compaction", "final"}
    )


def test_step_to_record_shape():
    s = Step(
        trajectory_id="traj_abc",
        episode_id="sess_abc",
        step_id="step_main_42",
        parent_step_id="step_main_41",
        turn_id="turn_main_5",
        agent_id="main",
        step_type="tool_action",
        observation_summary="saw files",
        action={"type": "tool_call", "tool": "edit_file", "args_summary": "path=..."},
        result_summary="ok",
        next_state_summary="edited",
        latency_ms=850,
        input_tokens=1000,
        output_tokens=200,
        cost=0.01,
        risk_level="medium",
    )
    rec = s.to_record()
    # 扁平字段
    assert rec["trajectory_id"] == "traj_abc"
    assert rec["episode_id"] == "sess_abc"
    assert rec["step_id"] == "step_main_42"
    assert rec["parent_step_id"] == "step_main_41"
    assert rec["step_type"] == "tool_action"
    assert rec["observation"] == "saw files"
    assert rec["action"] == {"type": "tool_call", "tool": "edit_file", "args_summary": "path=..."}
    assert rec["result"] == "ok"
    assert rec["next_state_summary"] == "edited"
    assert rec["reward"] is None
    assert rec["done"] is False
    # cost 子 dict（docs/10 示例：tokens = in + out）
    assert rec["cost"]["tokens"] == 1200
    assert rec["cost"]["latency_ms"] == 850
    # metadata block（含 branch_id：fork 分支身份随 step 投影保留）
    assert rec["metadata"] == {
        "agent_id": "main", "branch_id": "main",
        "turn_id": "turn_main_5", "risk_level": "medium",
    }


def test_step_defaults():
    s = Step(
        trajectory_id="t",
        episode_id="e",
        step_id="step_main_0",
        parent_step_id=None,
        turn_id=None,
        agent_id="main",
        step_type="llm_decision",
    )
    rec = s.to_record()
    assert rec["cost"]["tokens"] == 0
    assert rec["cost"]["latency_ms"] is None
    assert rec["metadata"]["risk_level"] == "low"
    assert rec["reward"] is None and rec["eval_result"] is None


def test_trajectory_metadata():
    m = TrajectoryMetadata(trajectory_id="traj_x", episode_id="sess_x", model="m")
    assert m.trajectory_id == "traj_x"
    assert m.episode_id == "sess_x"
    assert m.total_cost == 0.0
    assert m.n_steps == 0
