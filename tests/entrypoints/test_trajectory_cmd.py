"""`nanocode trajectory` 子命令 smoke + parse_args 接受 --trajectory 标志（docs/14 Milestone B2）。

覆盖：
- `nanocode trajectory`（缺省=list）在无会话时返回 0（容忍空）。
- `nanocode trajectory list` 返回 0。
- export 在一个真实 canonical 树会话上写出 bundle（4 文件齐全）并返回 0。
- show 在该会话上返回 0；坏 id 走 print_error 返回 1。
- parse_args 接受 --trajectory / --trajectory-level，并默认关闭。

直接调用 trajectory_cmd.run(argv)；conftest 已给隔离 NANOCODE_HOME(tmp)。
"""
from __future__ import annotations

import json

from nanocode.entrypoints.trajectory_cmd import run
from nanocode.entrypoints import cli
from nanocode.session.manager import session_root

from tests.trajectory import _fixtures as F


def _synth_session(session_id: str = "cmdsid") -> str:
    """构造一段最小的 canonical 树会话（llm 轮 + 工具往返 + 终结）。"""
    m = F.new_session(session_id)
    F.append_user(m, "do x")
    F.append_llm_request(m, model="claude-x", message_count=1, messages_chars=30)
    F.append_assistant(m, text="I will read",
                       tool_calls=[{"id": "tu-1", "name": "read_file",
                                    "arguments": {"file_path": "/a.py"}}],
                       input_tokens=10, output_tokens=4, latency_ms=100)
    F.append_tool_result(m, tool_call_id="tu-1", tool_name="read_file",
                         content="contents", latency_ms=50)
    F.append_assistant(m, text="done", tool_calls=[], input_tokens=2, output_tokens=1, latency_ms=20)
    F.append_session_end(m, final_status="completed")
    m.close()
    return session_id


# ─── list ─────────────────────────────────────────────────────────


def test_list_no_sessions_returns_zero():
    assert run([]) == 0
    assert run(["list"]) == 0


def test_list_with_session_returns_zero():
    _synth_session()
    assert run(["list"]) == 0


# ─── show ─────────────────────────────────────────────────────────


def test_show_synth_session_returns_zero():
    sid = _synth_session(session_id="showsid")
    assert run(["show", sid]) == 0
    assert run(["show", "latest"]) == 0


def test_show_bad_id_returns_one():
    assert run(["show", "nope-no-such-session"]) == 1


# ─── export ───────────────────────────────────────────────────────


def test_export_writes_bundle():
    sid = _synth_session(session_id="expsid")
    assert run(["export", sid]) == 0
    bundle = session_root(sid) / "trajectory"
    for name in ("metadata.json", "steps.jsonl", "metrics.json", "evals.jsonl"):
        assert (bundle / name).exists(), f"missing {name}"
    meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    assert meta.get("episode_id") == sid


def test_export_out_dir(tmp_path):
    sid = _synth_session(session_id="expsid2")
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
