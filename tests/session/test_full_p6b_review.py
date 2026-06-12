"""docs/14 full-P6b + P7-a review remediation 回归（4 项 high/medium）：
① 子 agent compaction 写进自己的 child 树（否则被 tree 重渲染抵消）；
② 子 agent 不消费父共享 TaskManager 的 finished-task 提醒；
③ skill_listing 树写失败时不推进 dedup（不静默丢清单）；
④ capture_anthropic 容忍 string assistant content（P7-a resume seeding 依赖）。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.session import capture
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _agent(sid, **kw):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions", **kw)


def test_subagent_compaction_writes_to_its_child_tree(monkeypatch):
    parent = _agent("CPX")
    sub = _agent("scx", is_sub_agent=True)
    sub._tree_session_id = parent.child_session_id("a1")
    sub._child_parent_session = {"sessionId": "CPX", "entryId": None, "taskId": "a1", "agentId": "a1"}
    sub._session_mgr = SessionManager.create(sub._tree_session_id, parent_session=sub._child_parent_session)
    sub._session_mgr.append_message(T.user_message("u"))      # leaf == last user

    async def fake():
        return "SUB-SUMMARY"

    monkeypatch.setattr(sub, "_compact_anthropic", fake)
    asyncio.run(sub._compact_conversation())
    comp = [e for e in sub._session_mgr.entries() if e.type == T.COMPACTION]
    assert len(comp) == 1 and comp[0].data["summary"] == "SUB-SUMMARY"


def test_subagent_does_not_consume_parent_finished_tasks():
    parent = _agent("STEAL")
    parent._session_mgr = SessionManager.create("STEAL")
    rec = parent.task_manager.create_task("shell", "bg", owner_agent_id=None)
    parent.task_manager.update_task(rec.id, status="completed", result_path="/x")
    sub = _agent("substeal", is_sub_agent=True)
    sub.task_manager = parent.task_manager                    # 与父共享 TaskManager
    sub._session_mgr = SessionManager.create("substeal.child")
    sub._inject_finished_tasks()                              # 子 → no-op（不偷父的提醒）
    assert parent.task_manager.get_task(rec.id).injected is False


def test_skill_listing_dedup_not_advanced_on_tree_write_failure(monkeypatch):
    a = _agent("SLF")
    a._session_mgr = SessionManager.create("SLF")
    monkeypatch.setattr("nanocode.agent.engine.skill_listing_delta", lambda s, act, b: ("LISTING", {"s1"}))
    monkeypatch.setattr(a, "_tree_custom_message", lambda *args, **kw: False)   # 模拟树写失败
    a._inject_skill_listing()
    assert "s1" not in a._sent_skill_names                    # 不推进 dedup → 下轮重试


def test_capture_anthropic_handles_string_assistant_content():
    out = capture.capture_anthropic({"role": "assistant", "content": "plain string"}, model="m")
    assert out[0]["content"] == [{"type": "text", "text": "plain string"}]
    # 空 string → 无块（不崩）
    assert capture.capture_anthropic({"role": "assistant", "content": ""}, model="m")[0]["content"] == []
