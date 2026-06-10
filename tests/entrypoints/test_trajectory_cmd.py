"""`nanocode trajectory` 子命令 smoke + parse_args 接受 --trajectory 标志。

覆盖：
- `nanocode trajectory`（缺省=list）在无会话时返回 0（容忍空）。
- `nanocode trajectory list` 返回 0。
- export 在一个合成 wire 会话上写出 bundle（4 文件齐全）并返回 0。
- show 在合成会话上返回 0；坏 id 走 print_error 返回 1。
- parse_args 接受 --trajectory / --trajectory-level，并默认关闭。

直接调用 trajectory_cmd.run(argv)；conftest 已给隔离 NANOCODE_HOME(tmp)。
"""
from __future__ import annotations

import json

from nanocode.entrypoints.trajectory_cmd import run
from nanocode.entrypoints import cli
from nanocode.session.v2 import agent_wire_path, session_root
from nanocode.trace.sinks import JsonlSink
from nanocode.trace.tracer import Tracer


def _synth_wire(session_id: str = "cmdsid", agent_id: str = "main") -> str:
    """写一段最小的 trajectory-enabled wire 到 <session>/agents/<agent>/wire.jsonl。"""
    wire = agent_wire_path(session_id, agent_id)
    tr = Tracer(session_id, [JsonlSink(wire)], agent_id=agent_id,
                trajectory_enabled=True, trajectory_level="full")
    tr.begin_turn("turn-1")
    tr.emit("session_start", model="claude-x")
    tr.emit("llm_request", model="claude-x", message_count=1,
            messages=[{"role": "user", "content": "do x"}])
    tr.emit("assistant_message", text="I will read", tool_uses=[{"id": "tu-1", "name": "read_file"}])
    tr.emit("llm_response", input_tokens=10, output_tokens=4)
    tr.emit("tool_call", tool="read_file", tool_use_id="tu-1", input={"file_path": "/a.py"})
    tr.emit("tool_result", tool="read_file", tool_use_id="tu-1", result="contents")
    tr.emit("assistant_message", text="done", tool_uses=[])
    tr.emit("session_end", final_status="completed")
    tr.close()
    return session_id


# ─── list ─────────────────────────────────────────────────────────


def test_list_no_sessions_returns_zero():
    assert run([]) == 0
    assert run(["list"]) == 0


def test_list_with_session_returns_zero():
    _synth_wire()
    assert run(["list"]) == 0


# ─── show ─────────────────────────────────────────────────────────


def test_show_synth_session_returns_zero():
    sid = _synth_wire(session_id="showsid")
    assert run(["show", sid]) == 0
    assert run(["show", "latest"]) == 0


def test_show_bad_id_returns_one():
    assert run(["show", "nope-no-such-session"]) == 1


# ─── export ───────────────────────────────────────────────────────


def test_export_writes_bundle():
    sid = _synth_wire(session_id="expsid")
    assert run(["export", sid]) == 0
    bundle = session_root(sid) / "trajectory"
    for name in ("metadata.json", "steps.jsonl", "metrics.json", "evals.jsonl"):
        assert (bundle / name).exists(), f"missing {name}"
    meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    assert meta.get("episode_id") == sid


def test_export_out_dir(tmp_path):
    sid = _synth_wire(session_id="expsid2")
    out = tmp_path / "mybundle"
    assert run(["export", sid, "--out", str(out)]) == 0
    assert (out / "steps.jsonl").exists()


def test_export_bad_id_returns_one():
    assert run(["export", "nope-no-such-session"]) == 1


# ─── parse_args accepts the new flags ─────────────────────────────


def test_parse_args_accepts_trajectory_flags(monkeypatch):
    monkeypatch.setattr("sys.argv", ["nanocode", "--trajectory", "--trajectory-level", "full"])
    args = cli.parse_args()
    assert args.trajectory is True
    assert args.trajectory_level == "full"


def test_parse_args_trajectory_defaults(monkeypatch):
    monkeypatch.setattr("sys.argv", ["nanocode"])
    args = cli.parse_args()
    assert args.trajectory is False
    assert args.trajectory_level is None
