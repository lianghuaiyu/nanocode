"""docs/15 Phase 3 cutover 契约：项目指令 + memory 静态段移出 system prompt → session-context
custom_message 注入（§8.3）。验证幂等(resume 不重复)、compaction 存活、子 agent 不注入、
system prompt 不再烤进这两块。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.lease import SessionLease


class _FakeUsage:
    input_tokens = 1
    output_tokens = 1


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResp:
    def __init__(self):
        self.content = [_FakeBlock("ok")]
        self.usage = _FakeUsage()


async def _fake_stream(**_kw):
    return _FakeResp()


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    a._provider.stream = _fake_stream
    return a


def _proj_entries(mgr):
    return [e for e in mgr.entries()
            if e.type == T.CUSTOM_MESSAGE and e.data.get("customType") == "project_instructions"]


def test_fresh_session_injects_project_instructions_once():
    a = _agent("cut1")
    asyncio.run(a.chat("hi"))
    assert len(_proj_entries(a._session_mgr)) == 1


def test_resume_same_session_does_not_reinject():
    a = _agent("cut2")
    asyncio.run(a.chat("hi"))
    asyncio.run(a.chat("again"))           # 同 session 第二轮：dedup,不重复注入
    assert len(_proj_entries(a._session_mgr)) == 1


def test_system_prompt_no_longer_bakes_project_or_memory():
    from nanocode.prompt import build_system_prompt
    s = build_system_prompt()
    assert "Project Instructions (NANOCODE.md" not in s     # 项目指令移出 system
    assert "# Memory System" not in s                        # memory 静态段移出 system
    assert "You are nanocode" in s                           # 稳定身份仍在 system


def test_subagent_does_not_get_session_context():
    a = _agent("cut3")
    a.is_sub_agent = True
    asyncio.run(a.chat("hi"))
    assert _proj_entries(a._session_mgr) == []               # 子 agent 不注入项目指令


def test_session_context_present_handles_compaction_survival():
    a = _agent("cut4")
    mgr = SessionLease.open_or_create("cut4").manager
    a._session_mgr = mgr
    mgr.append(T.CUSTOM_MESSAGE, {"customType": "project_instructions", "content": "X", "display": False})
    u = mgr.append_message(T.user_message("u1"))
    assert a._session_context_present() is True               # 无 compaction：proj 在 kept 区 → 跳过重注入
    mgr.append_compaction(summary="s", first_kept_entry_id=u.id)
    mgr.append_message(T.user_message("u2"))
    # proj 在 compaction 之前 → 被折出有效区间 → 视为缺失 → 下一轮会重注入（survival matrix）
    assert a._session_context_present() is False


def test_injected_pack_does_not_mutate_user_message():
    a = _agent("cut5")
    asyncio.run(a.chat("hello world"))
    msg_entries = [e for e in a._session_mgr.entries() if e.type == T.MESSAGE]
    first_user = msg_entries[0].data["message"]
    assert first_user["role"] == "user" and first_user["content"] == "hello world"   # §8.5：user 未被污染
