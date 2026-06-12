"""P6 /tree 命令：read-only 打印 canonical session 树（entries + 当前 leaf）。"""

import asyncio

from nanocode.agent import AgentSession
from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.builtin import _checkout, _fork, _resume, _rewind, _tree
from nanocode.entrypoints.commands.types import CommandContext
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _ctx(a):
    return CommandContext(agent=a, session=AgentSession(a), out=a._sink)


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def test_tree_command_prints_entries_and_leaf(capsys):
    a = _agent("treecmd")
    mgr = SessionManager.create("treecmd")
    mgr.append_message(T.user_message("hi"))
    mgr.append_message(T.assistant_message([T.text_block("yo")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    asyncio.run(_tree(_ctx(a), ""))
    out = capsys.readouterr().out
    assert "session tree" in out
    assert "message/user" in out and "message/assistant" in out
    assert "← leaf" in out


def test_tree_command_no_tree(capsys):
    a = _agent("notree")
    asyncio.run(_tree(_ctx(a), ""))
    assert "No canonical session tree" in capsys.readouterr().out


def _seed(a, sid):
    mgr = SessionManager.create(sid)
    a._session_mgr = mgr
    return mgr


def test_checkout_moves_leaf_and_reloads_context(capsys):
    a = _agent("co1")
    mgr = _seed(a, "co1")
    u1 = mgr.append_message(T.user_message("first"))
    mgr.append_message(T.assistant_message([T.text_block("r1")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(T.user_message("second"))
    asyncio.run(_checkout(_ctx(a), u1.id[-8:]))   # uuidv7 尾部唯一 handle
    out = capsys.readouterr().out
    assert "Checked out" in out
    assert mgr.get_leaf() == u1.id
    live = str(a.agent_session.build_request_messages())
    assert "first" in live and "second" not in live   # 上下文回到 first 之处


def test_rewind_to_before_last_user(capsys):
    a = _agent("rw1")
    mgr = _seed(a, "rw1")
    mgr.append_message(T.user_message("q1"))
    mgr.append_message(T.assistant_message([T.text_block("a1")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(T.user_message("q2-oops"))
    asyncio.run(_rewind(_ctx(a), ""))
    out = capsys.readouterr().out
    assert "Rewound" in out and "q2-oops" in out      # 打印旧文本供重输
    live = str(a.agent_session.build_request_messages())
    assert "q2-oops" not in live and "q1" in live      # 回到上一轮之前


def test_checkout_bad_id_fails_closed(capsys):
    a = _agent("co2")
    _seed(a, "co2").append_message(T.user_message("x"))
    asyncio.run(_checkout(_ctx(a), "ent_nonexistent"))
    assert "not found" in capsys.readouterr().out


def test_fork_emits_control_and_leaves_source_untouched():
    # pi /fork：handler 发 Control（新 session 由 runtime thread_fork 完成）；源 session 的
    # leaf/内容原样保留（同 session 内移 leaf 是 /tree <entry> 的职责）。
    from nanocode.entrypoints.commands.types import Control
    a = _agent("fk1")
    mgr = _seed(a, "fk1")
    mgr.append_message(T.assistant_message([T.text_block("a")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    u = mgr.append_message(T.user_message("q"))           # 无参 /fork → 选中此条
    res = asyncio.run(_fork(_ctx(a), ""))
    assert isinstance(res, Control) and res.payload["kind"] == "fork"
    assert res.payload["userEntryId"] == u.id and res.payload["prefill"] == "q"
    assert a.session_id == "fk1"                          # 不切换（runtime 才切换）
    assert a._session_mgr.get_leaf() == u.id              # 源 leaf 未动


def test_resume_lists_sessions(capsys):
    from nanocode.session.manager import SessionManager
    SessionManager.create("rs1").close()                  # canonical 树才进列表（docs/16 C-3）
    a = _agent("rs_cur")
    _seed(a, "rs_cur").append_message(T.user_message("hi"))
    asyncio.run(_resume(_ctx(a), ""))
    out = capsys.readouterr().out
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


def test_rewind_before_first_message_resets_to_root():
    # review low：post-3a 树首条 user 消息 parentId=None；/rewind 旧守卫 `not target.parentId` 会误判
    # 「无可回退」。修复后与 /fork 一致：move_to(None) → 复位 root（空上下文）。
    a = _agent("rwfirst")
    mgr = _seed(a, "rwfirst")
    mgr.append_message(T.user_message("only q"))      # 首条 user 消息，parentId=None
    asyncio.run(_rewind(_ctx(a), ""))
    assert a._session_mgr.get_leaf() is None          # 回退到 root（修复前被拒、leaf 不动）
