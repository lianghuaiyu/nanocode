"""docs/14 full-P6b：子 agent 把自己的 transcript **实时**写进独立 child session.jsonl（header 记
parentSession 血缘，artifacts 仍 parent-keyed）。children() 发现；/parent 导航父 session；
短前缀 /resume 不被 child sid 污染。"""

import asyncio

from nanocode.runtime import AgentConfig, AgentRuntime
from nanocode.session.agent import AgentSession
from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.types import CommandContext, Control
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager, children


def _agent(sid, **kw):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions", **kw)


def _ctx(a):
    return CommandContext(thread=AgentRuntime()._attach_agent(a))


def _sub_with_child(parent, agent_id, spawn_leaf):
    """复刻 _build_sub_agent + spawn 给子 agent 接上 child-tree 接线（不跑真实模型循环）：
    docs/14 SessionLease——spawn 给子 agent 注入一把 child 写者租约（locked child mgr）。"""
    from nanocode.session.lease import SessionLease
    parent._subagent_spawn_leaf[agent_id] = spawn_leaf
    sub = _agent("subx", is_sub_agent=True)
    sub._tree_session_id = parent.child_session_id(agent_id)
    sub._child_parent_session = {"sessionId": parent.session_id, "entryId": spawn_leaf,
                                 "taskId": agent_id, "agentId": agent_id}
    sub._session_mgr = SessionLease.open_or_create(
        sub._tree_session_id, spawned_by=sub._child_parent_session).manager
    return sub


def test_subagent_writes_child_session_live_with_lineage():
    parent = _agent("PARENT")
    parent._session_mgr = SessionManager.create("PARENT")
    spawn_leaf = parent._session_mgr.append_message(T.user_message("do task")).id
    sub = _sub_with_child(parent, "agent-001", spawn_leaf)
    sub.agent_session.record_provider_messages({"role": "user", "content": "sub prompt"})                       # 实时写 child 树
    sub.agent_session.record_provider_messages({"role": "assistant", "content": [{"type": "text", "text": "sub done"}]})
    parent._close_child_session("agent-001", sub)                                  # close child mgr
    child_sid = parent.child_session_id("agent-001")
    assert SessionManager.exists(child_sid)
    child = SessionManager.open(child_sid)
    ps = child.spawned_by()
    assert ps["sessionId"] == "PARENT" and ps["entryId"] == spawn_leaf
    assert ps["taskId"] == "agent-001" and ps["agentId"] == "agent-001"
    contents = str([e.data for e in child.entries() if e.type == T.MESSAGE])
    assert "sub prompt" in contents and "sub done" in contents
    assert child_sid in children("PARENT")                                            # children() 发现
    # 父树**不**含子 transcript（只 bounded result 经父 tool_result 入父树，此处父树仅 do task）
    parent_contents = str([e.data for e in parent._session_mgr.entries() if e.type == T.MESSAGE])
    assert "sub prompt" not in parent_contents and "sub done" not in parent_contents


def test_subagent_child_tree_accumulates_across_runs():
    # 同 agent_id 第二次运行（resume）打开已存在 child → 追加，不重建 header。
    parent = _agent("PARENT2")
    parent._session_mgr = SessionManager.create("PARENT2")
    sub1 = _sub_with_child(parent, "a1", None)
    sub1.agent_session.record_provider_messages({"role": "user", "content": "one"})
    sub1.agent_session.record_provider_messages({"role": "assistant", "content": "done"})
    parent._close_child_session("a1", sub1)
    sub2 = _sub_with_child(parent, "a1", None)
    sub2.agent_session.record_provider_messages({"role": "user", "content": "two"})
    parent._close_child_session("a1", sub2)
    child = SessionManager.open(parent.child_session_id("a1"))
    assert sum(1 for e in child.entries() if e.type == T.SESSION_START) == 1          # 单 header
    msgs = [e.data["message"]["content"] for e in child.entries() if e.type == T.MESSAGE]
    assert msgs == ["one", [{"type": "text", "text": "done"}], "two"]                  # 累积


def test_empty_subagent_does_not_materialize_child_session():
    # Pi 对齐：child 也在首个 assistant 前延迟落盘。空/取消的 child 不应成为可恢复会话。
    parent = _agent("EP")
    parent._session_mgr = SessionManager.create("EP")
    sub = _sub_with_child(parent, "a-empty", None)
    parent._close_child_session("a-empty", sub)
    child_sid = parent.child_session_id("a-empty")
    assert not SessionManager.exists(child_sid)


def test_build_subagent_child_session_uses_runtime_cwd(tmp_path):
    cwd = tmp_path / "child-cwd"
    cwd.mkdir()
    rt = AgentRuntime()
    th = rt.thread_start(AgentConfig(api_key="test", session_id="PCWD",
                                     permission_mode="bypassPermissions",
                                     cwd=str(cwd)))
    parent = th._agent
    try:
        sub = parent._build_sub_agent(system_prompt="s", tools=[],
                                      agent_type="coder", artifact_id="agent-cwd")
        sub.agent_session.record_provider_messages({"role": "user", "content": "check cwd"})
        sub.agent_session.record_provider_messages({"role": "assistant", "content": "ok"})
        parent._close_child_session("agent-cwd", sub)
        child = SessionManager.open(parent.child_session_id("agent-cwd"))
        assert child._cwd() == str(cwd.resolve())
    finally:
        th.release_lease()


def test_child_session_id_navigates_to_child():
    from nanocode.entrypoints.commands.builtin import _agent as _agent_cmd
    parent = _agent("AGNAV")
    parent._session_mgr = SessionManager.create("AGNAV")
    SessionManager.create("sess_child_nav", spawned_by={"sessionId": "AGNAV", "entryId": None})
    res = asyncio.run(_agent_cmd(_ctx(parent), "sess_child_nav"))
    assert isinstance(res, Control) and res.payload["sessionId"] == "sess_child_nav"


def test_agent_next_cycles_into_child_from_parent():
    from nanocode.entrypoints.commands.builtin import _agent as _agent_cmd
    parent = _agent("AGNEXT")
    parent._session_mgr = SessionManager.create("AGNEXT")
    SessionManager.create("AGNEXT.a1", spawned_by={"sessionId": "AGNEXT", "entryId": None})
    SessionManager.create("AGNEXT.a2", spawned_by={"sessionId": "AGNEXT", "entryId": None})
    res = asyncio.run(_agent_cmd(_ctx(parent), "next"))
    assert isinstance(res, Control) and res.payload["sessionId"] in ("AGNEXT.a1", "AGNEXT.a2")


def test_agent_unknown_id_prints_detail():
    from nanocode.entrypoints.commands.builtin import _agent as _agent_cmd
    parent = _agent("AGDET")
    parent._session_mgr = SessionManager.create("AGDET")
    res = asyncio.run(_agent_cmd(_ctx(parent), "nonexistent"))
    assert not isinstance(res, Control)   # 无对应 child → 打印详情（Local）


def test_resume_short_prefix_resolves_parent_not_polluted_by_child():
    # P6 review #5：child sid 与父共享前缀；短前缀 /resume 应唯一解析到父（child 仅 exact id 可达）。
    from nanocode.entrypoints.commands.builtin import _resume
    SessionManager.create("abcd1234")
    SessionManager.create("abcd1234.agent-001", spawned_by={"sessionId": "abcd1234", "entryId": None})
    a = _agent("other")
    a._session_mgr = SessionManager.create("other")
    res = asyncio.run(_resume(_ctx(a), "abcd"))          # 短前缀 → 父（child 被排除）
    assert isinstance(res, Control) and res.payload["sessionId"] == "abcd1234"
    res2 = asyncio.run(_resume(_ctx(a), "abcd1234.agent-001"))   # child 仍可 exact 进入
    assert isinstance(res2, Control) and res2.payload["sessionId"] == "abcd1234.agent-001"
