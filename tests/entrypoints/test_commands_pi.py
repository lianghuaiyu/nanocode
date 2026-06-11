"""docs/14 P4：命令 Pi 对齐 —— /name(session_info)、/clone(跨文件复制)、/fork(before-user fork)、
/tree <entry>(导航)。handler 只 resolve + 发 Control；runtime 经 thread_clone/thread_fork 原子切换。"""

import asyncio

from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread
from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.builtin import _clone, _fork, _name, _tree
from nanocode.entrypoints.commands.types import CommandContext, Control
from nanocode.entrypoints.host import RuntimeHost
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


def _host(sid):
    a = _agent(sid)
    a._session_mgr = SessionManager.create(sid)
    rt = AgentRuntime()
    t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    return a, rt, t, RuntimeHost(rt, t, registry=None)


def _ctx(a):
    return CommandContext(agent=a, session=AgentSession(a), out=a._sink)


# ─── /name ─────────────────────────────────────────────────────────────────
def test_name_set_show_clear(capsys):
    a, rt, t, host = _host("NAMESID")
    asyncio.run(_name(_ctx(a), "my session"))
    assert a._session_mgr.name() == "my session"
    asyncio.run(_name(_ctx(a), ""))                      # 无参显示
    assert "my session" in capsys.readouterr().out
    asyncio.run(_name(_ctx(a), "--clear"))               # tombstone
    assert a._session_mgr.name() is None


def test_name_does_not_move_leaf():
    a, rt, t, host = _host("NAMELEAF")
    u = a._session_mgr.append_message(T.user_message("hi"))
    asyncio.run(_name(_ctx(a), "foo"))
    assert a._session_mgr.get_leaf() == u.id             # session_info 不推进 leaf


# ─── /clone ────────────────────────────────────────────────────────────────
def test_clone_handler_returns_control():
    a, rt, t, host = _host("CH")
    a._session_mgr.append_message(T.user_message("x"))
    res = asyncio.run(_clone(_ctx(a), ""))
    assert isinstance(res, Control) and res.action == "replace_thread"
    assert res.payload["kind"] == "clone" and res.payload["sourceSid"] == "CH"


def test_thread_clone_creates_child_with_parent_session_and_switches():
    a, rt, t, host = _host("CLONESRC")
    a._session_mgr.append_message(T.user_message("q1"))
    a._session_mgr.append_message(T.assistant_message([T.text_block("a1")], provider="anthropic",
                                  api="anthropic", model="claude-x", stop_reason="stop"))
    new_t = rt.thread_clone(host, "CLONESRC")
    assert new_t is not None and host.current_thread is new_t
    child_sid = a.session_id
    assert child_sid != "CLONESRC"
    ps = SessionManager.open(child_sid).parent_session()
    assert ps and ps["sessionId"] == "CLONESRC"          # parentSession 血缘
    assert "q1" in str(a._anthropic_messages)            # path-to-root 复制过来


# ─── /fork（Pi before-user）─────────────────────────────────────────────────
def test_fork_handler_defaults_to_last_user():
    a, rt, t, host = _host("FH")
    a._session_mgr.append_message(T.user_message("only-q"))
    res = asyncio.run(_fork(_ctx(a), ""))
    assert isinstance(res, Control) and res.action == "fork"
    assert res.payload["selectedEntryId"] and res.payload["sourceSid"] == "FH"


def test_thread_fork_before_user_excludes_it_and_returns_text():
    a, rt, t, host = _host("FORKSRC")
    mgr = a._session_mgr
    mgr.append_message(T.user_message("first q"))
    mgr.append_message(T.assistant_message([T.text_block("first a")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    u2 = mgr.append_message(T.user_message("second q"))   # fork before this
    new_t, selected = rt.thread_fork(host, "FORKSRC", u2.id)
    assert new_t is not None and host.current_thread is new_t
    assert selected == "second q"                         # 选中文本回显
    live = str(a._anthropic_messages)
    assert "first q" in live and "first a" in live        # 之前对话 clone 过去
    assert "second q" not in live                         # 选中 user 消息不含（fork 之前）


def test_thread_fork_before_first_message_yields_empty_child():
    a, rt, t, host = _host("FORKFIRST")
    u1 = a._session_mgr.append_message(T.user_message("only q"))   # branch root
    new_t, selected = rt.thread_fork(host, "FORKFIRST", u1.id)
    assert new_t is not None and selected == "only q"
    assert a._anthropic_messages == []                    # fork 到空（其前无内容）
    assert a.session_id != "FORKFIRST"


# ─── /tree <entry> 导航 ──────────────────────────────────────────────────────
def test_tree_entry_navigates_moves_leaf(capsys):
    a, rt, t, host = _host("TN")
    u1 = a._session_mgr.append_message(T.user_message("first"))
    a._session_mgr.append_message(T.user_message("second"))
    asyncio.run(_tree(_ctx(a), u1.id[-8:]))               # /tree <entry> → move_to
    assert a._session_mgr.get_leaf() == u1.id
    assert "first" in str(a._anthropic_messages) and "second" not in str(a._anthropic_messages)
