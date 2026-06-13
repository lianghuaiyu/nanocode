"""命令 pi 语义对齐 —— /name(session_info)、/clone(新 session：复制 active branch 到当前 leaf、
编辑器空)、/fork(新 session：复制到选中 user 消息之前、该 prompt 预填编辑器)、/tree <entry>
(同 session 内移 leaf)。/clone、/fork 经 Control→thread_clone/thread_fork 原子切换；
/tree 是 in-file（经 AgentSession.move_to）。"""

import asyncio

from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread
from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.builtin import _clone, _fork, _name, _tree
from nanocode.entrypoints.commands.types import CommandContext, Control, Local
from nanocode.entrypoints.host import RuntimeHost
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def _host(sid):
    a = _agent(sid)
    a._session_mgr = SessionManager.create(sid)
    rt = AgentRuntime()
    t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    return a, rt, t, RuntimeHost(rt, t, registry=None)


def _ctx(a):
    return CommandContext(thread=AgentRuntime().adopt(a))


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
    assert "q1" in str(a.agent_session.build_request_messages())            # path-to-root 复制过来


# ─── /fork（in-file before-user：移 leaf，不新建 session）────────────────────────
def test_fork_no_arg_returns_control_with_last_user_and_prefill():
    # pi /fork：无参 = 最近一条 user 消息；handler 发 Control（新建 session 由 runtime 完成），
    # payload 携带选中 entry + 预填文本（该 prompt 放回编辑器）。
    a, rt, t, host = _host("FORKSRC")
    mgr = a._session_mgr
    mgr.append_message(T.user_message("first q"))
    mgr.append_message(T.assistant_message([T.text_block("first a")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    u2 = mgr.append_message(T.user_message("second q"))
    res = asyncio.run(_fork(_ctx(a), ""))
    assert isinstance(res, Control) and res.action == "replace_thread"
    assert res.payload["kind"] == "fork" and res.payload["sourceSid"] == "FORKSRC"
    assert res.payload["userEntryId"] == u2.id
    assert res.payload["prefill"] == "second q"
    assert a.session_id == "FORKSRC"                        # handler 只发信号，不切换


def test_thread_fork_copies_prefix_into_new_session():
    # runtime.thread_fork：复制到选中 user 消息**之前** → 新 session 切入；原 session 保留。
    a, rt, t, host = _host("FORKAT")
    mgr = a._session_mgr
    mgr.append_message(T.user_message("q1"))
    mgr.append_message(T.assistant_message([T.text_block("a1")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    u2 = mgr.append_message(T.user_message("q2 SELECTED"))
    new_t = rt.thread_fork(host, "FORKAT", u2.id)
    assert new_t is not None and host.current_thread is new_t
    assert a.session_id != "FORKAT"                          # 新 session
    live = str(a.agent_session.build_request_messages())
    assert "q1" in live and "a1" in live                     # 选中消息之前的内容复制过来
    assert "q2 SELECTED" not in live                         # 选中消息及其后不复制
    assert SessionManager.exists("FORKAT")                   # 原 session 保留
    # 血缘（pi：fork 两条路径 header 形状一致）：sessionId + entryId(=复制前缀 tip) + forkedBeforeEntryId
    from nanocode.session.manager import children
    assert a.session_id in children("FORKAT")
    ps = SessionManager.open(a.session_id).parent_session()
    assert ps["sessionId"] == "FORKAT"
    assert ps["forkedBeforeEntryId"] == u2.id


def test_thread_fork_before_first_message_yields_empty_new_session_with_lineage():
    a, rt, t, host = _host("FORKFIRST")
    u1 = a._session_mgr.append_message(T.user_message("only q"))   # branch root（parentId=None）
    new_t = rt.thread_fork(host, "FORKFIRST", u1.id)
    assert new_t is not None
    assert a.session_id != "FORKFIRST"                       # 之前无内容 → 全新空 session
    assert a.agent_session.build_request_messages() == []
    # review P1：空前缀路径**也**记 parentSession 血缘——children/parent 导航与审计依赖它，
    # 且 header 形状与非空前缀路径一致（pi：空前缀 fork 仍是 fork，不是 new）。
    from nanocode.session.manager import children
    assert a.session_id in children("FORKFIRST")
    ps = SessionManager.open(a.session_id).parent_session()
    assert ps["sessionId"] == "FORKFIRST"
    assert ps["forkedBeforeEntryId"] == u1.id


def test_thread_fork_rejects_non_user_entries_fail_closed():
    # review P2：runtime facade 自己强制 user-message 校验（SDK/AppServer 可绕过 CLI handler 直调）。
    a, rt, t, host = _host("FORKBAD")
    mgr = a._session_mgr
    mgr.append_message(T.user_message("q"))
    a1 = mgr.append_message(T.assistant_message([T.text_block("a")], provider="anthropic",
                            api="anthropic", model="claude-x", stop_reason="stop"))
    comp = mgr.append_compaction(summary="s", first_kept_entry_id=None)
    assert rt.thread_fork(host, "FORKBAD", a1.id) is None      # assistant entry → 拒绝
    assert rt.thread_fork(host, "FORKBAD", comp.id) is None    # compaction entry → 拒绝
    assert rt.thread_fork(host, "FORKBAD", "no-such-entry") is None
    assert a.session_id == "FORKBAD"                           # fail-closed：未切换


def test_clone_rejects_arguments():
    # pi /clone：固定复制当前 branch 到当前 leaf，无参数（按 entry 分叉用 /fork）。
    a, rt, t, host = _host("CLONEARG")
    a._session_mgr.append_message(T.user_message("x"))
    res = asyncio.run(_clone(_ctx(a), "someentry"))
    assert isinstance(res, Local)


# ─── /tree <entry> 导航 ──────────────────────────────────────────────────────
def test_tree_entry_navigates_moves_leaf(capsys):
    a, rt, t, host = _host("TN")
    u1 = a._session_mgr.append_message(T.user_message("first"))
    a._session_mgr.append_message(T.user_message("second"))
    asyncio.run(_tree(_ctx(a), u1.id[-8:]))               # /tree <entry> → move_to
    assert a._session_mgr.get_leaf() == u1.id
    assert "first" in str(a.agent_session.build_request_messages()) and "second" not in str(a.agent_session.build_request_messages())


def test_fork_invalid_target_lists_user_message_candidates(capsys):
    # pi 双层收窄的 UX 层：选错目标时打印 user 消息候选（getUserMessagesForForking 的文本等价）。
    a, rt, t, host = _host("FORKCAND")
    mgr = a._session_mgr
    mgr.append_message(T.user_message("pick me one"))
    a1 = mgr.append_message(T.assistant_message([T.text_block("nope")], provider="anthropic",
                            api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(T.user_message("pick me two"))
    res = asyncio.run(_fork(_ctx(a), a1.id[-8:]))             # 选了 assistant entry
    assert isinstance(res, Local)
    out = capsys.readouterr().out
    assert "must be a user message" in out
    assert "pick me one" in out and "pick me two" in out      # 候选清单（近期在前）
    assert "nope" not in out                                  # 非 user 不进候选
