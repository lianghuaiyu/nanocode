"""docs/14 P3 review remediation 回归：pre-3a 盘上树（首消息 parentId 指向 session_start）下的
clone/fork 不污染、不产空 fork；tree-only resume 在树空时回退 legacy；tree 仅含注入（无真实
MESSAGE）时 _build_request_messages 回退 flat（不挤掉真实 user 消息）。
"""

from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread
from nanocode.agent.engine import Agent
from nanocode.entrypoints.host import RuntimeHost
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager, session_file
from nanocode.session.tree import Entry


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


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


def test_fork_pre3a_first_message_yields_clean_empty_child():
    mgr, start_id, u1 = _seed_pre3a_tree("pre3a_fork")
    a = _agent("pre3a_fork")
    a._session_mgr = mgr
    rt = AgentRuntime()
    t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    host = RuntimeHost(rt, t)
    # fork before the FIRST user message (its parentId points at session_start → treated as None)
    new_t, selected = rt.thread_fork(host, "pre3a_fork", u1.id)
    assert new_t is not None and selected == "first q"
    child = SessionManager.open(a.session_id)
    starts = [e for e in child.entries() if e.type == T.SESSION_START]
    assert len(starts) == 1                                  # 干净空 child（无双 session_start）
    assert child.build_context().messages == []             # fork 到空（其前无内容）


def test_restore_empty_tree_falls_back_to_legacy():
    # 树存在但只有 header（build_context 空）→ resume 用 legacy 兜底，不静默丢历史（P3 review #4）。
    SessionManager.create("emptytree")                       # header-only 树
    a = _agent("emptytree")
    a.model = "claude-x"
    a.restore_session({"anthropicMessages": [{"role": "user", "content": "legacy-hist"}]})
    assert "legacy-hist" in str(a._anthropic_messages)       # 空树未遮蔽 legacy


def test_build_request_falls_back_to_flat_when_tree_has_only_injection():
    # 模拟 user 消息 _tree_record 失败但注入成功：树只有 custom_message、无真实 MESSAGE →
    # _build_request_messages 回退 flat（保住真实 user 消息，不只发注入文本，P3 review #8）。
    a = _agent("onlyinject")
    mgr = SessionManager.create("onlyinject")
    a._session_mgr = mgr
    a._anthropic_messages = [{"role": "user", "content": "IMPORTANT USER QUESTION"}]
    mgr.append(T.CUSTOM_MESSAGE, {"customType": "skill_listing", "content": "SKILL-LISTING", "display": False})
    req = a._build_request_messages()
    joined = str(req)
    assert "IMPORTANT USER QUESTION" in joined               # 真实 user 消息未被挤掉
