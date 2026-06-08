"""Task 5: persistent subagent 注册 + 持久化 + agent 工具重写。

- fresh 路径：注册 SubAgentRecord（agent-001，type 归一，status running→completed），
  messages 落 v2.read_agent_messages(sid, "agent-001")，token 累加到父。
- resume 路径：reload 历史 + 追加新 prompt；unknown id 报错；provider mismatch 报错；
  model mismatch 报错；不新增记录。
- run_in_background=True 返回未支持错误。
"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.session import v2 as _session_v2


def _agent(**kw):
    return Agent(api_key="test", trace_enabled=False,
                 permission_mode="bypassPermissions", session_id="psid", **kw)


def _stub_run_once(agent, text="sub done", history=None):
    """Stub run_once on a specific Agent instance: 写入 messages 历史 + 返回固定文本/token。"""
    async def _ro(prompt: str) -> dict:
        agent._anthropic_messages.append({"role": "user", "content": prompt})
        agent._anthropic_messages.append({"role": "assistant", "content": text})
        if history is not None:
            history.append(prompt)
        return {"text": text, "tokens": {"input": 11, "output": 7}}
    return _ro


# ─── fresh 路径：注册 + 持久化 + token 累加 ──────────────────


def test_agent_tool_registers_subagent_record(monkeypatch):
    parent = _agent()
    built = {}

    real_build = parent._build_sub_agent

    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub)
        built["sub"] = sub
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)

    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "do x", "prompt": "build it"}))

    # P3: parent gets a bounded envelope (small text passes through as summary) that
    # also points at the persisted result.md — not the raw transcript verbatim.
    assert "sub done" in res
    assert "result.md" in res
    recs = parent.task_manager.list_subagents()
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "agent-001"
    assert rec.type == "coder"
    assert rec.status == "completed"


def test_agent_tool_persists_messages_to_v2(monkeypatch):
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub, text="persisted body")
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))

    msgs = _session_v2.read_agent_messages("psid", "agent-001")
    assert any(m.get("content") == "persisted body" for m in msgs)


def test_agent_tool_token_accumulation(monkeypatch):
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub)
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    before_in = parent.total_input_tokens
    before_out = parent.total_output_tokens
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert parent.total_input_tokens == before_in + 11
    assert parent.total_output_tokens == before_out + 7


def test_agent_tool_type_normalized(monkeypatch):
    """未知 type 归一为 coder（general 语义）。"""
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub)
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p"}))  # no type → general → normalized coder
    rec = parent.task_manager.list_subagents()[0]
    assert rec.type == "coder"


# ─── resume 路径 ─────────────────────────────────────────────


def test_resume_reloads_history_and_appends(monkeypatch):
    parent = _agent()
    # 预置一个已持久化的子 agent（fresh 一次）
    real_build = parent._build_sub_agent

    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub, text="first")
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "first prompt"}))
    first_msgs = _session_v2.read_agent_messages("psid", "agent-001")
    assert len(first_msgs) >= 2

    # resume：reload 历史，追加新 prompt
    reloaded = {}

    def _spy_build2(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            reloaded["history_len_before_run"] = len(sub._anthropic_messages)
            sub._anthropic_messages.append({"role": "user", "content": prompt})
            sub._anthropic_messages.append({"role": "assistant", "content": "second"})
            return {"text": "second", "tokens": {"input": 3, "output": 2}}

        sub.run_once = _ro
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build2)
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "second prompt", "resume": "agent-001"}))
    # P3: resume also returns the bounded envelope (summary passthrough + result_path).
    assert "second" in res
    assert "result.md" in res
    # resume 不新增记录
    assert len(parent.task_manager.list_subagents()) == 1
    # run_once 之前历史已 reload（>=2 条）
    assert reloaded["history_len_before_run"] >= 2


def test_resume_unknown_id_errors():
    parent = _agent()
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "resume": "agent-999"}))
    assert "agent-999" in res
    assert "unknown" in res.lower() or "not found" in res.lower()
    assert parent.task_manager.list_subagents() == []


def test_resume_provider_mismatch_errors(monkeypatch):
    parent = _agent()
    # 注册一个 provider=openai 的记录，但父是 anthropic
    rec = parent.task_manager.create_subagent(
        type="coder", description="d", model=parent.model, provider="openai")
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "resume": rec.id}))
    assert "provider" in res.lower()
    assert "mismatch" in res.lower() or "cannot" in res.lower()


def test_resume_model_mismatch_errors(monkeypatch):
    parent = _agent()
    rec = parent.task_manager.create_subagent(
        type="coder", description="d", model="some-other-model",
        provider=parent._current_provider())
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "resume": rec.id}))
    assert "model" in res.lower()
    assert "mismatch" in res.lower() or "cannot" in res.lower()


# ─── run_in_background 现已支持（阶段 E）：立即返回 task_id ────


def test_run_in_background_starts_task(monkeypatch):
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub)
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "run_in_background": True}))
    assert "background" in res.lower()
    assert "task-001" in res
    # 立即注册 task + subagent（双向链）
    assert parent.task_manager.get_task("task-001") is not None
    assert parent.task_manager.get_subagent("agent-001") is not None


def test_run_in_background_with_resume_errors():
    parent = _agent()
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p",
         "run_in_background": True, "resume": "agent-001"}))
    assert "resume" in res.lower()
    assert "run_in_background" in res.lower() or "background" in res.lower()
    assert parent.task_manager.list_subagents() == []


# ─── _current_provider 基础 ──────────────────────────────────


def test_current_provider_anthropic():
    parent = _agent()
    assert parent._current_provider() == "anthropic"


def test_current_provider_openai():
    parent = Agent(api_key="test", trace_enabled=False,
                   permission_mode="bypassPermissions",
                   api_base="https://example.com/v1", session_id="osid")
    assert parent._current_provider() == "openai"
