"""P3：AgentSession 会话层 seam + SessionContextBuilder（快照源，P5 将换事件树）。"""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.session import AgentSession
from nanocode.agent.context_builder import SessionContextBuilder
from nanocode.session import v2 as _v2


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", trace_enabled=False, session_id="p3sid", **kw)


def test_run_turn_delegates_to_chat():
    a = _agent()
    seen = []

    async def fake_chat(prompt):
        seen.append(prompt)

    a.chat = fake_chat
    s = AgentSession(a)
    asyncio.run(s.run_turn("hello"))
    assert seen == ["hello"]            # run_turn 委托 chat，行为不变
    assert s.session_id == a.session_id
    assert s.aborted is a._aborted


def test_context_builder_reads_main_snapshot():
    a = _agent()
    _v2.write_main_messages(a.session_id, [{"role": "user", "content": "snap"}])
    b = SessionContextBuilder(a.session_id)
    assert b.resume_messages() == [{"role": "user", "content": "snap"}]
    assert b.resume_messages(agent_id="agent-001") == []  # 无快照 → 空


def test_session_resume_loads_via_builder_into_store():
    a = _agent()
    _v2.write_main_messages(a.session_id, [{"role": "user", "content": "prior"}])
    s = AgentSession(a)
    loaded = s.resume()
    assert loaded == [{"role": "user", "content": "prior"}]
    # 装入 agent 的活动 MessageStore（anthropic 默认）
    assert a._anthropic_messages == [{"role": "user", "content": "prior"}]


def test_session_resume_empty_snapshot_does_not_clobber():
    a = _agent()
    a._append_message({"role": "user", "content": "live"})
    s = AgentSession(a)
    s.resume()  # 无快照 → 不覆盖现有
    assert a._anthropic_messages == [{"role": "user", "content": "live"}]


def test_builder_injectable_into_session():
    a = _agent()

    class FakeBuilder(SessionContextBuilder):
        def resume_messages(self, *, agent_id="main"):
            return [{"role": "user", "content": f"built:{agent_id}"}]

    s = AgentSession(a, context_builder=FakeBuilder(a.session_id))
    s.resume()
    assert a._anthropic_messages == [{"role": "user", "content": "built:main"}]
