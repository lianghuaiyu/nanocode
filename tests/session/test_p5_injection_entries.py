"""P5a：注入作为 custom_message tree entry（additive），build_context 折入、render 合并进 user 消息。"""

from nanocode.agent import AgentSession
from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager
from nanocode.session.render import ModelCtx, render

ANTH = ModelCtx("anthropic", "anthropic", "claude-x")


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def test_tree_custom_message_folds_and_merges_into_user():
    a = _agent("p5a")
    mgr = SessionManager.create("p5a")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("hi"))
    a.agent_session._tree_custom_message("skill_listing", "<system-reminder>SKILLS</system-reminder>")
    assert any(e.type == T.CUSTOM_MESSAGE for e in mgr.entries())
    payload = render(mgr.build_context().messages, ANTH)["messages"]
    assert [m["role"] for m in payload] == ["user"]          # 注入并入同一条 user（合并相邻 user）
    joined = str(payload[0]["content"])
    assert "hi" in joined and "SKILLS" in joined


def test_tree_custom_message_guarded_no_mgr():
    a = _agent("p5a2")          # 无 _session_mgr → 静默跳过、不抛
    a.agent_session._tree_custom_message("skill_listing", "X")
    assert not SessionManager.exists("p5a2")


def test_inject_skill_listing_writes_custom_message(monkeypatch):
    a = _agent("p5a3")
    mgr = SessionManager.create("p5a3")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("hi"))
    monkeypatch.setattr("nanocode.skills.listing.skill_listing_delta",
                        lambda sent, act, budget: ("LISTING-X", {"s1"}))
    a.agent_session.inject_skill_listing()                                      # tree-backed → 只写树 custom_message
    cms = [e for e in mgr.entries() if e.type == T.CUSTOM_MESSAGE]
    assert len(cms) == 1 and "LISTING-X" in cms[0].data["content"]


def test_subagent_injection_writes_to_child_tree():
    # docs/14 full-P6b：子 agent（持 child _session_mgr）的注入写进**它自己的** child 树（按 _session_mgr gate）。
    a = _agent("p5a4")
    a.is_sub_agent = True
    mgr = SessionManager.create("p5a4")    # 代表子的 child tree
    a._session_mgr = mgr
    assert a.agent_session._tree_custom_message("skill_listing", "X")
    assert any(e.type == T.CUSTOM_MESSAGE for e in mgr.entries())


def test_injection_does_not_mutate_prior_user_message_entry():
    # docs/14 §4.5 核心不变量：注入是独立 custom_message entry，绝不改写已有 user MESSAGE entry。
    a = _agent("p5b")
    mgr = SessionManager.create("p5b")
    a._session_mgr = mgr
    u = mgr.append_message(T.user_message("original user text"))
    a.agent_session._tree_custom_message("skill_listing", "INJECTED-REMINDER")
    user_entry = next(e for e in mgr.entries() if e.id == u.id)
    assert user_entry.data["message"]["content"] == "original user text"   # 原 user entry 未变
    cms = [e for e in mgr.entries() if e.type == T.CUSTOM_MESSAGE]
    assert len(cms) == 1 and cms[0].data["content"] == "INJECTED-REMINDER"  # 独立 entry
    # 存储层分离、render 投影时才合并进 user
    payload = render(mgr.build_context().messages, ANTH)["messages"]
    assert "original user text" in str(payload) and "INJECTED-REMINDER" in str(payload)


def test_inject_finished_tasks_tree_backed_writes_custom_message_not_flat(monkeypatch):
    # 主 agent（有树）→ finished-tasks 写 custom_message、不改 flat list。
    a = _agent("p5c")
    mgr = SessionManager.create("p5c")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("hi"))
    monkeypatch.setattr("nanocode.tasks.inject.collect_pending_injections",
                        lambda tm: [type("FakeT", (), {"id": "t1"})()])
    monkeypatch.setattr("nanocode.tasks.inject.render_task_reminder", lambda t: "TASK-DONE")
    a.agent_session.inject_finished_tasks()
    cms = [e for e in mgr.entries() if e.type == T.CUSTOM_MESSAGE]
    assert any("TASK-DONE" in e.data.get("content", "") for e in cms)
