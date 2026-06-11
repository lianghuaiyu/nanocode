"""tests for trajectory.export.export_bundle / bundle_dir（P5 导出 bundle，docs/14 Milestone B2）。

用真实 ``SessionManager`` 树（tests.trajectory._fixtures）构造一个会话（含一个工具往返 +
终结遥测），可选 child session（多 agent fan-out），再调 ``export_bundle`` 把树派生产物导出为
bundle，断言：
- 4 个文件（metadata.json / steps.jsonl / metrics.json / evals.jsonl）齐全。
- steps.jsonl 是合法 JSONL（逐行 json.loads），每行带 step 必备键。
- metrics.json 解析为 dict 且含已知指标键。
- metadata.json 解析、episode_id == session_id、n_steps == steps.jsonl 行数。
- 多 agent（child session）的 step 都进 merged bundle。
- 缺失 session 仍产出合法空 bundle。

conftest 已给每个测试隔离的 NANOCODE_HOME(tmp)，session/bundle 自动落该 tmp 下。
"""
from __future__ import annotations

import json

from nanocode.session.manager import session_root
from nanocode.trajectory import bundle_dir, export_bundle

from tests.trajectory import _fixtures as F


def _drive_full_agent(session_id: str):
    """在父会话上跑一段树：llm 轮 + 一个工具往返 + 终结遥测。返回该 mgr（已 close）。"""
    m = F.new_session(session_id)
    F.append_user(m, "do x")
    F.append_llm_request(m, model="claude-x", message_count=2, messages_chars=120)
    F.append_assistant(m, text="I will read a file",
                       tool_calls=[{"id": "tu-1", "name": "read_file",
                                    "arguments": {"file_path": "/a.py"}}],
                       input_tokens=120, output_tokens=42, latency_ms=300)
    F.append_tool_result(m, tool_call_id="tu-1", tool_name="read_file",
                         content="file contents here", latency_ms=80)
    F.append_assistant(m, text="done", tool_calls=[],
                       input_tokens=5, output_tokens=2, latency_ms=40)
    F.append_turn_end(m, input_tokens=125, output_tokens=44, turns=1)
    F.append_session_end(m, final_status="completed")
    return m


def _drive_child_agent(parent, session_id: str, agent_id: str = "agent-001"):
    """child session（多 agent fan-out）：跑 pytest run_shell 往返。"""
    c = F.child_session(parent, session_id, agent_id=agent_id)
    F.append_user(c, "run tests")
    F.append_llm_request(c, model="claude-x", message_count=3, messages_chars=200)
    F.append_assistant(c, text="run a shell command",
                       tool_calls=[{"id": "tu-9", "name": "run_shell",
                                    "arguments": {"command": "pytest -q"}}],
                       input_tokens=80, output_tokens=10, latency_ms=500)
    F.append_tool_result(c, tool_call_id="tu-9", tool_name="run_shell",
                         content="1 passed in 0.1s\n(exit 0)", latency_ms=900)
    F.append_turn_end(c, input_tokens=80, output_tokens=10, turns=1)
    return c


def _read_jsonl(path) -> list:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ── bundle_dir 路径契约 ─────────────────────────────────────────────


def test_bundle_dir_default_is_separate_subdir():
    sid = "sess-bundle-dir"
    b = bundle_dir(sid)
    assert b == session_root(sid) / "trajectory"
    assert "agents" not in b.parts


def test_bundle_dir_out_dir_override(tmp_path):
    b = bundle_dir("sess-x", tmp_path / "custom")
    assert b == tmp_path / "custom"


# ── 导出：FULL 级单 agent ───────────────────────────────────────────


def test_export_bundle_writes_four_files_and_valid_content():
    sid = "sess-full-export"
    _drive_full_agent(sid).close()

    out = export_bundle(sid)

    assert out == session_root(sid) / "trajectory"
    md = out / "metadata.json"
    steps = out / "steps.jsonl"
    metrics = out / "metrics.json"
    evals = out / "evals.jsonl"
    assert md.exists() and steps.exists() and metrics.exists() and evals.exists()

    step_records = _read_jsonl(steps)
    assert step_records, "expected at least one projected step"
    for rec in step_records:
        assert "step_id" in rec
        assert "step_type" in rec
        assert "action" in rec and isinstance(rec["action"], dict)
        assert "cost" in rec and isinstance(rec["cost"], dict)
        assert "metadata" in rec and "agent_id" in rec["metadata"]

    # tool_action 的 args_summary 来自真实 input（含文件路径），不是占位。
    tool_steps = [r for r in step_records if r["step_type"] == "tool_action"]
    assert tool_steps
    assert any("/a.py" in (s["action"].get("args_summary") or "") for s in tool_steps)

    m = json.loads(metrics.read_text(encoding="utf-8"))
    assert isinstance(m, dict)
    for key in ("total_tool_calls", "total_input_tokens", "est_cost_usd", "per_agent", "per_tool"):
        assert key in m
    assert m["total_tool_calls"] >= 1
    assert m["total_input_tokens"] == 125  # turn_end 累计

    meta = json.loads(md.read_text(encoding="utf-8"))
    assert meta["episode_id"] == sid
    assert meta["trajectory_id"] == f"traj_{sid}"
    assert meta["model"] == "claude-x"
    assert meta["n_steps"] == len(step_records)
    assert meta["final_status"] == "completed"
    assert meta["total_input_tokens"] == 125

    eval_records = _read_jsonl(evals)
    assert all(isinstance(e, dict) and "signal" in e for e in eval_records)


def test_export_bundle_merges_multiple_agents():
    sid = "sess-multi-agent"
    p = _drive_full_agent(sid)
    c = _drive_child_agent(p, "sess-multi-agent-child", agent_id="agent-001")
    p.close()
    c.close()

    out = export_bundle(sid)
    step_records = _read_jsonl(out / "steps.jsonl")
    agents = {r["metadata"]["agent_id"] for r in step_records}
    assert "main" in agents
    assert "agent-001" in agents

    # child agent 跑了 pytest run_shell → tests_run 启发式应识别（命令在 tool_call 端、树存全量）。
    m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert m["tests_run"] >= 1


def test_out_dir_override_writes_there(tmp_path):
    sid = "sess-out-override"
    _drive_full_agent(sid).close()
    dest = tmp_path / "exported"
    out = export_bundle(sid, dest)
    assert out == dest
    assert (dest / "steps.jsonl").exists()
    assert not (session_root(sid) / "trajectory" / "steps.jsonl").exists()


def test_missing_session_still_exports_empty_bundle():
    out = export_bundle("never-existed")
    for fname in ("metadata.json", "steps.jsonl", "metrics.json", "evals.jsonl"):
        assert (out / fname).exists()
    assert _read_jsonl(out / "steps.jsonl") == []
    assert _read_jsonl(out / "evals.jsonl") == []
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["n_steps"] == 0
    assert meta["episode_id"] == "never-existed"


def test_export_attaches_reward_to_failing_step_in_steps_jsonl():
    """回归（codex 复审）：export 必须在写 steps.jsonl 前 attach_rewards——否则导出的 reward
    恒为 null。造一个失败 tool 轮，断言对应 tool_action step 的 reward == -1.0 落进 steps.jsonl，
    成功 final step 的 reward 保持 null。"""
    sid = "sess-reward-export"
    m = F.new_session(sid)
    F.append_user(m, "run it")
    F.append_llm_request(m, model="claude-x", message_count=1, messages_chars=20)
    F.append_assistant(m, text="run it",
                       tool_calls=[{"id": "tu-1", "name": "run_shell",
                                    "arguments": {"command": "boom"}}],
                       input_tokens=10, output_tokens=3, latency_ms=200)
    F.append_tool_result(m, tool_call_id="tu-1", tool_name="run_shell",
                         content="Error: boom", is_error=True, latency_ms=50)
    F.append_assistant(m, text="all done", tool_calls=[],
                       input_tokens=3, output_tokens=1, latency_ms=30)
    F.append_turn_end(m, input_tokens=13, output_tokens=4, turns=1)
    F.append_session_end(m, final_status="completed")
    m.close()

    out = export_bundle(sid)
    recs = _read_jsonl(out / "steps.jsonl")
    tool_steps = [r for r in recs if r["step_type"] == "tool_action"]
    final_steps = [r for r in recs if r["step_type"] == "final"]
    assert tool_steps and any(r["reward"] == -1.0 for r in tool_steps), \
        "failing tool_action step must carry the attached reward in steps.jsonl"
    assert all(r["reward"] is None for r in final_steps), \
        "successful final step must not be penalized"
