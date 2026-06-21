"""docs/14 P3 review remediation 回归：pre-3a 盘上树（首消息 parentId 指向 session_start）下的
clone 不污染（无双 session_start）；in-file /fork before 首消息 → 复位到空上下文（同 session）。

（原「空树回退 legacy」「树仅注入回退 flat」两个用例已删——SessionLease 下无 flat fallback、
canonical 树是唯一权威；见文件末说明。）
"""

from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread
from nanocode.agent.engine import Agent
from nanocode.entrypoints.host import RuntimeHost
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager, session_file
from nanocode.session.tree import Entry


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def _seed_pre3a_tree(sid):
    """手写一棵 pre-3a 形状的树：首消息 parentId 指向 session_start（旧 leaf-advancing 语义）。"""
    mgr = SessionManager.create(sid)
    start_id = mgr.entries()[0].id
    u1 = mgr.append(T.MESSAGE, {"message": T.user_message("first q")}, parent_id=start_id)
    mgr.append(T.MESSAGE, {"message": T.assistant_message([T.text_block("first a")], provider="anthropic",
               api="anthropic", model="claude-x", stop_reason="stop")}, parent_id=u1.id)
    return mgr, start_id, u1


def test_clone_pre3a_tree_no_double_session_start():
    mgr, start_id, u1 = _seed_pre3a_tree("pre3a_clone")
    child = mgr.clone()
    starts = [e for e in child.entries() if e.type == T.SESSION_START]
    assert len(starts) == 1                                  # 只有 child 自己的 header，无复制来的第二条
    # 首条复制消息成为干净 branch root（parentId=None），不再指向被剥的 session_start
    msgs = [e for e in child.entries() if e.type == T.MESSAGE]
    assert msgs[0].parentId is None
    assert len(msgs) == 2 and msgs[0].data["message"]["content"] == "first q"
    assert msgs[1].data["message"]["role"] == "assistant"


def test_fork_pre3a_first_message_yields_empty_new_session():
    # pi /fork + pre-3a 盘上树（首消息 parentId 指向 session_start）：fork before 首消息 →
    # 前缀只剩 session_start（剥掉后无可复制）→ runtime 落到全新空 session。
    import asyncio
    from nanocode.agent import AgentRuntime, RuntimeThread
    from nanocode.entrypoints.commands.builtin import _fork
    from nanocode.entrypoints.commands.types import CommandContext, Control
    from nanocode.entrypoints.host import RuntimeHost
    mgr, start_id, u1 = _seed_pre3a_tree("pre3a_fork")
    a = _agent("pre3a_fork")
    a._session_mgr = mgr
    ctx = CommandContext(thread=AgentRuntime()._attach_agent(a))
    res = asyncio.run(_fork(ctx, u1.id[-8:]))
    assert isinstance(res, Control) and res.payload["kind"] == "fork"
    rt = AgentRuntime()
    t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    host = RuntimeHost(rt, t, registry=None)
    new_t = rt.thread_fork(host, "pre3a_fork", res.payload["userEntryId"])
    assert new_t is not None
    assert a.session_id != "pre3a_fork"                      # 新 session
    assert a.agent_session.build_request_messages() == []    # 其前无内容 → 空上下文
    from nanocode.session.manager import children
    new_sid = a.session_id
    assert new_sid not in children("pre3a_fork")             # 空 fork 首个 assistant 前不污染 children()
    a.agent_session.record_provider_messages({"role": "user", "content": "followup"})
    a.agent_session.record_provider_messages({"role": "assistant", "content": "ok"})
    assert new_sid in children("pre3a_fork")                 # materialize 后 lineage 可发现


# docs/14 SessionLease：删除「空树回退 legacy」与「树仅 custom_message 回退 flat」两个用例——
# 二者断言的 flat fallback 已彻底移除（canonical 树是唯一权威；user 消息先于 build_request 入树；
# restore_session 已退役）。无 flat fallback、缺 lease 即 fatal，是新设计的硬不变量。
