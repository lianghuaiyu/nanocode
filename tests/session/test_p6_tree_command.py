"""P6 /tree 命令：read-only 打印 canonical session 树（entries + 当前 leaf）。"""

import asyncio

from nanocode.agent import AgentRuntime, AgentSession
from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.builtin import _checkout, _resume, _tree
from nanocode.entrypoints.commands.types import CommandContext
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _ctx(a):
    return CommandContext(thread=AgentRuntime()._attach_agent(a))


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def test_tree_command_prints_entries_and_leaf(capsys):
    a = _agent("treecmd")
    mgr = SessionManager.create("treecmd")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("hi"))
    mgr.append_message(T.assistant_message([T.text_block("yo")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    res = asyncio.run(_tree(_ctx(a), ""))
    out = res.output or ""                       # docs/18 step 5：结构化 output
    assert "session tree" in out
    assert "user: hi" in out and "assistant: yo" in out
    assert "◀ current" in out


def test_tree_command_tool_rows_do_not_get_assistant_prefix():
    a = _agent("treecmd-tools")
    mgr = SessionManager.create("treecmd-tools")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("run tests"))
    mgr.append_message(T.assistant_message(
        [T.tool_call_block("tc1", "run_shell", {"command": "pytest -q"})],
        provider="anthropic", api="anthropic", model="claude-x", stop_reason="toolUse"))
    mgr.append_message(T.tool_result_message(
        tool_call_id="tc1", tool_name="run_shell", content="passed"))
    res = asyncio.run(_tree(_ctx(a), ""))
    out = res.output or ""
    assert "[run_shell: pytest -q]" in out
    assert "assistant: [run_shell" not in out
    assert "assistant: (no content)" not in out


def test_tree_command_no_tree():
    a = _agent("notree")
    res = asyncio.run(_tree(_ctx(a), ""))
    assert "No canonical session tree" in (res.output or "")


def _seed(a, sid):
    mgr = SessionManager.create(sid)
    a._session_mgr = mgr
    return mgr


def test_checkout_moves_leaf_and_reloads_context():
    a = _agent("co1")
    mgr = _seed(a, "co1")
    u1 = mgr.append_message(T.user_message("first"))
    mgr.append_message(T.assistant_message([T.text_block("r1")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(T.user_message("second"))
    res = asyncio.run(_checkout(_ctx(a), u1.id[-8:]))   # uuidv7 尾部唯一 handle
    out = res.output or ""
    assert "Checked out" in out
    assert mgr.get_leaf() is None
    assert res.prefill == "first"
    assert a.agent_session.build_request_messages() == []   # user 选择回到 parent，并把文本交回编辑器


def test_checkout_bad_id_fails_closed():
    a = _agent("co2")
    _seed(a, "co2").append_message(T.user_message("x"))
    res = asyncio.run(_checkout(_ctx(a), "ent_nonexistent"))
    assert "not found" in (res.output or "")


# /fork handler 的 Control payload / prefill / 源不动 钉点在 tests/entrypoints/test_commands_pi.py
# （test_fork_no_arg_returns_control_with_last_user_and_prefill 等）——此处不再重复。


def test_resume_lists_sessions():
    from nanocode.session.manager import SessionManager
    rs1 = SessionManager.create("rs1")
    rs1.append_message(T.user_message("saved"))
    rs1.append_message(T.assistant_message([T.text_block("ok")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    rs1.close()
    a = _agent("rs_cur")
    _seed(a, "rs_cur").append_message(T.user_message("hi"))
    res = asyncio.run(_resume(_ctx(a), ""))               # 无参非交互 → 嵌套文本列表
    out = res.output or ""                                # docs/18 step 5：结构化 output
    assert "Resumable sessions" in out
    assert "rs1" in out and "rs_cur" in out and "← current" in out


def test_resume_id_returns_resume_control():
    # docs/14 P2：/resume <id> handler 只 resolve 候选并返回 Control("resume")；真正切换由 runtime
    # 经 _apply_control → thread_resume 完成（见 tests/entrypoints/test_thread_lifecycle.py）。
    from nanocode.entrypoints.commands.types import Control
    target = _agent("tgt")
    tmgr = SessionManager.create("tgt")
    target._session_mgr = tmgr
    tmgr.append_message(T.user_message("target-conversation"))
    a = _agent("cur2")
    _seed(a, "cur2").append_message(T.user_message("current"))
    res = asyncio.run(_resume(_ctx(a), "tgt"))
    assert isinstance(res, Control)
    assert res.action == "resume" and res.payload.get("sessionId") == "tgt"
    assert a.session_id == "cur2"                     # handler 不切换（切换在 runtime 层）
