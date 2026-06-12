"""AgentSession 会话层 seam：run_turn 委托 chat、move_to in-file 导航。

docs/14 P7：SessionContextBuilder（P3 快照 / P5 事件树重建）已退役——resume 由
Agent.rebind_session/restore_session 从 canonical session.jsonl 重建（见 tests/agent/
test_rebind_session.py、tests/session/test_p3_resume.py），fork 由 runtime thread_fork（Pi
before-user fork，tests/entrypoints/test_commands_pi.py）承担。故本文件只保留会话层 seam 测试。
"""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.session import AgentSession
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="p3sid", **kw)


def test_chat_delegates_to_run_turn():
    # docs/16 #3c 方向反转：turn shell 在 AgentSession.run_turn，Agent.chat 是薄公开入口。
    a = _agent()
    seen = []

    async def fake_run_turn(prompt):
        seen.append(prompt)

    a.agent_session.run_turn = fake_run_turn
    asyncio.run(a.chat("hello"))
    assert seen == ["hello"]
    s = AgentSession(a)
    assert s.session_id == a.session_id
    assert s.aborted is a._aborted


def test_move_to_navigates_in_file_and_reloads():
    a = _agent()
    mgr = a._session_mgr = SessionManager.create("p3sid")
    u1 = mgr.append_message(T.user_message("first"))
    mgr.append_message(T.user_message("second"))
    s = AgentSession(a)
    s.move_to(u1.id)                    # in-file 导航回 first
    assert mgr.get_leaf() == u1.id
    assert "first" in str(a.agent_session.build_request_messages()) and "second" not in str(a.agent_session.build_request_messages())


def test_move_to_unknown_entry_fails_closed():
    a = _agent()
    a._session_mgr = SessionManager.create("p3sid")
    import pytest
    with pytest.raises(ValueError):
        s = AgentSession(a)
        s.move_to("ent_nonexistent")
