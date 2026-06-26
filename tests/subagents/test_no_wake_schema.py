"""A3 (docs/25 §4)：删除误导性的 `wake` 半契约。

`wake=True` 对非 running 子曾直接抛错（schema 宣传可唤醒 idle 子，实现却拒绝）。删除后做
Codex 式对称两态：steer/run_send 只对 **live**（非终态）子注入，对 terminal 子报 "use resume"；
resume 才重水合终态子。本测试守 schema 不再暴露 `wake`，且 steer-on-terminal 走 "use resume"。
"""
import pytest

from nanocode.tools import agent as agent_tool
from nanocode.tools import run_send as run_send_tool


def test_agent_schema_has_no_wake():
    props = agent_tool.SCHEMA["input_schema"]["properties"]
    assert "wake" not in props


def test_run_send_schema_has_no_wake():
    props = run_send_tool.SCHEMA["input_schema"]["properties"]
    assert "wake" not in props


def test_steer_on_terminal_run_says_use_resume():
    """对称两态：终态子 steer → "use resume"（queue_steer 不再有 wake 旁路）。"""
    from nanocode.session.lease import SessionLease
    from nanocode.subagents import run_record
    from nanocode.subagents.steer import queue_steer

    cid = "WAKE_CONTRACT_CHILD"
    lease = SessionLease.open_or_create(cid)
    try:
        run_record.create_run_record(
            child_session_id=cid, parent_session_id="P", spawn_entry_id=None,
            tool_call_id=None, agent_type="coder", description="d", background=True,
            context_mode="fresh", isolation="shared", worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"}, prompt="p")
        run_record.complete_run(cid, status="completed", result="done",
                                prompt_entry_id=None, result_entry_id=None)
    finally:
        lease.close()

    with pytest.raises(RuntimeError, match="use resume"):
        queue_steer(cid, "more", delivery="steer")
