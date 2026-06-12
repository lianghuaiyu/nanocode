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


def test_present_kinds_uses_fold_rendered_folded_out_reinjects():
    a = _agent("cut4")
    mgr = SessionLease.open_or_create("cut4").manager
    a._session_mgr = mgr
    mgr.append(T.CUSTOM_MESSAGE, {"customType": "project_instructions", "content": "X", "display": False})
    u = mgr.append_message(T.user_message("u1"))
    assert a._session_context_present_kinds() == {"project_instructions"}     # 无 compaction:渲染中
    # compaction firstKept=u1 → project_instructions(在 u1 之前)被折出渲染 → 视为缺失 → 会重注入(survival)
    mgr.append_compaction(summary="s", first_kept_entry_id=u.id)
    mgr.append_message(T.user_message("u2"))
    assert a._session_context_present_kinds() == set()


def test_present_kinds_keeps_pre_compaction_kept_region_no_double_inject():
    # codex-found：custom 在 compaction 的 kept 前区(firstKeptEntryId=None=全保留)→ fold 仍渲染 → 不重复注入。
    a = _agent("cut4b")
    mgr = SessionLease.open_or_create("cut4b").manager
    a._session_mgr = mgr
    mgr.append(T.CUSTOM_MESSAGE, {"customType": "project_instructions", "content": "X", "display": False})
    mgr.append_message(T.user_message("u1"))
    mgr.append_compaction(summary="s", first_kept_entry_id=None)              # None → fold 保留全部前区
    mgr.append_message(T.user_message("u2"))
    assert "project_instructions" in a._session_context_present_kinds()       # 仍渲染 → 不 double-inject


def test_per_customtype_dedup_not_suppressed_by_other_kind():
    # codex-found：仅 memory_static 在场时,project_instructions 不应被「另一 kind 在场」抑制(per-kind)。
    a = _agent("cut4c")
    mgr = SessionLease.open_or_create("cut4c").manager
    a._session_mgr = mgr
    mgr.append(T.CUSTOM_MESSAGE, {"customType": "memory_static", "content": "M", "display": False})
    present = a._session_context_present_kinds()
    assert present == {"memory_static"}
    assert "project_instructions" not in present                             # → 仍会被注入


def test_injected_pack_does_not_mutate_user_message():
    a = _agent("cut5")
    asyncio.run(a.chat("hello world"))
    msg_entries = [e for e in a._session_mgr.entries() if e.type == T.MESSAGE]
    first_user = msg_entries[0].data["message"]
    assert first_user["role"] == "user" and first_user["content"] == "hello world"   # §8.5：user 未被污染
