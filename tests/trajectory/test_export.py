"""tests for trajectory.export.export_bundle / bundle_dir（P5 导出 bundle）。

用真实 ``trace.tracer.Tracer`` + ``trace.sinks.JsonlSink`` 写到 ``session.v2.agent_wire_path``
的 per-agent wire（FULL 与 SUMMARY 两级、多个 agent），再调 ``export_bundle`` 把 merged wire
导出为 bundle，断言：
- 4 个文件（metadata.json / steps.jsonl / metrics.json / evals.jsonl）齐全。
- steps.jsonl 是合法 JSONL（逐行 json.loads），每行带 step 必备键。
- metrics.json 解析为 dict 且含已知指标键。
- metadata.json 解析、episode_id == session_id、n_steps == steps.jsonl 行数。
- SUMMARY 级 session 也能导出（不崩，且用 summary 字段——args/result 降级为占位）。

conftest 已给每个测试隔离的 NANOCODE_HOME(tmp)，wire/bundle 自动落该 tmp 下。
"""
from __future__ import annotations

import json

from nanocode.session.v2 import agent_wire_path, session_root
from nanocode.trace.sinks import JsonlSink
from nanocode.trace.tracer import Tracer
from nanocode.trajectory import bundle_dir, export_bundle


def _tracer(session_id: str, agent_id: str, *, level: str) -> Tracer:
    """构造一个写到 <session>/agents/<agent_id>/wire.jsonl 的 trajectory-enabled Tracer。"""
    wire = agent_wire_path(session_id, agent_id)
    return Tracer(
        session_id,
        [JsonlSink(wire)],
        agent_id=agent_id,
        trajectory_enabled=True,
        trajectory_level=level,
    )


def _drive_full_agent(session_id: str, agent_id: str = "main") -> None:
    """在一个 agent 上跑一段 FULL 级 trajectory：llm 轮 + 一个工具往返 + 终结。"""
    tr = _tracer(session_id, agent_id, level="full")
    tr.begin_turn("turn-1")
    tr.emit("llm_request", model="claude-x", message_count=2,
            messages=[{"role": "user", "content": "do x"}])
    tr.emit("assistant_message", text="I will read a file",
            tool_uses=[{"id": "tu-1", "name": "read_file"}])
    tr.emit("llm_response", input_tokens=120, output_tokens=42)
    tr.emit("tool_call", tool="read_file", tool_use_id="tu-1",
            input={"file_path": "/a.py"})
    tr.emit("tool_result", tool="read_file", tool_use_id="tu-1",
            result="file contents here")
    tr.emit("assistant_message", text="done", tool_uses=[])
    tr.emit("turn_end", final_status="completed")
    tr.emit("session_end", final_status="completed")
    tr.close()


def _drive_summary_agent(session_id: str, agent_id: str) -> None:
    """在另一个 agent 上跑 SUMMARY 级：heavy payload 应被 apply_summary_shaping 丢弃。"""
    tr = _tracer(session_id, agent_id, level="summary")
    tr.begin_turn("turn-1")
    tr.emit("llm_request", model="claude-x", message_count=3,
            messages=[{"role": "user", "content": "x" * 5000}])
    tr.emit("assistant_message", text="run a shell command",
            tool_uses=[{"id": "tu-9", "name": "run_shell"}])
    tr.emit("llm_response", input_tokens=80, output_tokens=10)
    tr.emit("tool_call", tool="run_shell", tool_use_id="tu-9",
            input={"command": "pytest -q"})
    tr.emit("tool_result", tool="run_shell", tool_use_id="tu-9",
            result="1 passed in 0.1s\n(exit 0)")
    tr.emit("turn_end", final_status="completed")
    tr.close()


def _read_jsonl(path) -> list:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ── bundle_dir 路径契约 ─────────────────────────────────────────────


def test_bundle_dir_default_is_separate_subdir():
    sid = "sess-bundle-dir"
    b = bundle_dir(sid)
    # 默认 = <session_root>/trajectory，独立子目录、绝不在 agents/* 下。
    assert b == session_root(sid) / "trajectory"
    assert "agents" not in b.parts


def test_bundle_dir_out_dir_override(tmp_path):
    b = bundle_dir("sess-x", tmp_path / "custom")
    assert b == tmp_path / "custom"


# ── 导出：FULL 级单 agent + summary 级第二 agent ────────────────────


def test_export_bundle_writes_four_files_and_valid_content():
    sid = "sess-full-export"
    _drive_full_agent(sid, "main")

    out = export_bundle(sid)

    # 默认 bundle 目录 = <session>/trajectory。
    assert out == session_root(sid) / "trajectory"
    md = out / "metadata.json"
    steps = out / "steps.jsonl"
    metrics = out / "metrics.json"
    evals = out / "evals.jsonl"
    assert md.exists() and steps.exists() and metrics.exists() and evals.exists()

    # steps.jsonl 合法 JSONL，每行带 step 必备键。
    step_records = _read_jsonl(steps)
    assert step_records, "expected at least one projected step"
    for rec in step_records:
        assert "step_id" in rec
        assert "step_type" in rec
        assert "action" in rec and isinstance(rec["action"], dict)
        assert "cost" in rec and isinstance(rec["cost"], dict)
        assert "metadata" in rec and "agent_id" in rec["metadata"]

    # FULL 级：tool_action 的 args_summary 应来自真实 input（含文件路径），不是占位。
    tool_steps = [r for r in step_records if r["step_type"] == "tool_action"]
    assert tool_steps
    assert any("/a.py" in (s["action"].get("args_summary") or "") for s in tool_steps)

    # metrics.json 解析为 dict，含已知指标键。
    m = json.loads(metrics.read_text(encoding="utf-8"))
    assert isinstance(m, dict)
    for key in ("total_tool_calls", "total_input_tokens", "est_cost_usd", "per_agent", "per_tool"):
        assert key in m
    assert m["total_tool_calls"] >= 1
    assert m["total_input_tokens"] == 120

    # metadata.json 解析、episode_id == session_id、n_steps == steps 行数。
    meta = json.loads(md.read_text(encoding="utf-8"))
    assert meta["episode_id"] == sid
    assert meta["trajectory_id"] == f"traj_{sid}"
    assert meta["model"] == "claude-x"
    assert meta["n_steps"] == len(step_records)
    assert meta["final_status"] == "completed"
    assert meta["total_input_tokens"] == 120

    # evals.jsonl 合法 JSONL（可空，但 FULL 跑有 reached_final_answer / touched? 等信号）。
    eval_records = _read_jsonl(evals)
    assert all(isinstance(e, dict) and "signal" in e for e in eval_records)


def test_export_bundle_merges_multiple_agents():
    sid = "sess-multi-agent"
    _drive_full_agent(sid, "main")
    _drive_summary_agent(sid, "agent-001")

    out = export_bundle(sid)
    step_records = _read_jsonl(out / "steps.jsonl")
    # 两个 agent 的 step 都应出现在 merged bundle 里。
    agents = {r["metadata"]["agent_id"] for r in step_records}
    assert "main" in agents
    assert "agent-001" in agents


# ── SUMMARY 级 session 仍可导出（用 summary 字段，不崩）──────────────


def test_summary_level_session_exports_without_crash():
    sid = "sess-summary-only"
    _drive_summary_agent(sid, "main")

    out = export_bundle(sid)
    assert (out / "metadata.json").exists()
    assert (out / "metrics.json").exists()

    step_records = _read_jsonl(out / "steps.jsonl")
    assert step_records, "summary-level session should still project steps"

    # SUMMARY 级：llm_request 的 messages 被丢弃，tool_result 的 result 被丢弃。
    # tool_action 的 result_summary 来自 summary 字段（result_summary），不是空也不报错。
    tool_steps = [r for r in step_records if r["step_type"] == "tool_action"]
    assert tool_steps
    # observation 是从 summary 的 messages_chars 派生（chars=...），不依赖被丢弃的 messages。
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["total_tool_calls"] >= 1
    # tests_run 启发式应识别 pytest 命令（命令在 tool_call 端，FULL 级保留）。
    assert metrics["tests_run"] >= 1


def test_out_dir_override_writes_there(tmp_path):
    sid = "sess-out-override"
    _drive_full_agent(sid, "main")
    dest = tmp_path / "exported"
    out = export_bundle(sid, dest)
    assert out == dest
    assert (dest / "steps.jsonl").exists()
    # 默认目录不应被写入（override 生效）。
    assert not (session_root(sid) / "trajectory" / "steps.jsonl").exists()


def test_missing_session_still_exports_empty_bundle():
    out = export_bundle("never-existed")
    for fname in ("metadata.json", "steps.jsonl", "metrics.json", "evals.jsonl"):
        assert (out / fname).exists()
    # 空 session：steps / evals 为空，metrics 为零值 dict。
    assert _read_jsonl(out / "steps.jsonl") == []
    assert _read_jsonl(out / "evals.jsonl") == []
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["n_steps"] == 0
    assert meta["episode_id"] == "never-existed"


def test_export_attaches_reward_to_failing_step_in_steps_jsonl():
    """回归（codex 复审）：export 必须在写 steps.jsonl 前 attach_rewards——否则导出的
    reward 恒为 null，P4 形同虚设。造一个失败 tool 轮，断言对应 tool_action step 的
    reward == -1.0 落进 steps.jsonl，而成功 final step 的 reward 保持 null。"""
    sid = "sess-reward-export"
    tr = _tracer(sid, "main", level="full")
    tr.begin_turn("turn-1")
    tr.emit("llm_request", model="claude-x", message_count=1,
            messages=[{"role": "user", "content": "run it"}])
    tr.emit("assistant_message", text="run it",
            tool_uses=[{"id": "tu-1", "name": "run_shell"}])
    tr.emit("llm_response", input_tokens=10, output_tokens=3)
    tr.emit("tool_call", tool="run_shell", tool_use_id="tu-1", input={"command": "boom"})
    tr.emit("tool_result", tool="run_shell", tool_use_id="tu-1", result="Error: boom")
    tr.emit("assistant_message", text="all done", tool_uses=[])
    tr.emit("turn_end", final_status="completed")
    tr.emit("session_end", final_status="completed")
    tr.close()

    out = export_bundle(sid)
    recs = _read_jsonl(out / "steps.jsonl")
    tool_steps = [r for r in recs if r["step_type"] == "tool_action"]
    final_steps = [r for r in recs if r["step_type"] == "final"]
    assert tool_steps and any(r["reward"] == -1.0 for r in tool_steps), \
        "failing tool_action step must carry the attached reward in steps.jsonl"
    assert all(r["reward"] is None for r in final_steps), \
        "successful final step must not be penalized"
