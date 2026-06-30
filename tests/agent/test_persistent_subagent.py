"""Persistent subagent child sessions + run records.

- fresh 路径：创建 child session id + run record，
  messages 实时落 child canonical 树，token 累加到父。
- resume 路径：reload 历史 + 追加新 prompt；unknown id 报错；provider mismatch 报错；
  model mismatch 报错；不新增记录。
- run_in_background=True 返回 child-session run id。
"""

import asyncio
import json
import shutil

import pytest

from nanocode.agent.engine import Agent
from .._helpers import inject_test_services


def _agent(**kw):
    _injected_agent = Agent(api_key="test",
                 permission_mode="bypassPermissions", session_id="psid", **kw)
    inject_test_services(_injected_agent)
    return _injected_agent


def _stub_run_once(agent, text="sub done", history=None):
    """Stub run_once on a specific Agent instance: 写入 messages 历史 + 返回固定文本/token。

    docs/14 SessionLease：真实 run_once 会把消息写进（child）树——stub 也落树（agent 有 child 租约时），
    使 resume 能从 child 树重载历史（docs/16 C-1：messages.json 副本已删，child 树是唯一历史）。"""
    async def _ro(prompt: str) -> dict:
        if agent._session_mgr is not None:
            agent.agent_session.record_provider_messages({"role": "user", "content": prompt})
            agent.agent_session.record_provider_messages({"role": "assistant", "content": text})
        if history is not None:
            history.append(prompt)
        return {"text": text, "tokens": {"input": 11, "output": 7}}
    return _ro


def _only_run_id(parent) -> str:
    recs = json.loads(parent.run_list())
    assert len(recs) == 1
    return recs[0]["child_session_id"]


# ─── fresh 路径：注册 + 持久化 + token 累加 ──────────────────


def test_agent_tool_registers_run_record(monkeypatch):
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
    recs = json.loads(parent.run_list())
    assert len(recs) == 1
    rec = recs[0]
    assert rec["child_session_id"].startswith("sess_")
    assert rec["agent_type"] == "coder"
    assert rec["status"] == "completed"
    out = json.loads(parent.run_output(rec["child_session_id"]))
    assert out["childSessionId"] == rec["child_session_id"]
    assert out["status"] == "completed"
    assert "sub done" in out["result"]


def test_agent_tool_writes_replayable_parent_task_envelope(monkeypatch):
    parent = _agent()
    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager
    from nanocode.subagents import run_record

    parent._session_mgr = SessionManager.create(parent.session_id)
    spawn_leaf = parent._session_mgr.append_message(T.user_message("spawn a child")).id
    real_build = parent._build_sub_agent

    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub, text="enveloped result")
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "write envelope", "prompt": "p"}))

    rec = json.loads(parent.run_list())[0]
    child_id = rec["child_session_id"]
    assert rec["run_id"] == child_id
    assert rec["task_id"].startswith("task_")
    assert rec["task_id"] != child_id

    child = SessionManager.open(child_id)
    spawned_by = child.spawned_by()
    assert spawned_by["agentId"] == child_id
    assert spawned_by["taskId"] == rec["task_id"]

    task_events = [e for e in parent._session_mgr.entries() if e.type == T.TASK_EVENT]
    assert [e.data["event"] for e in task_events] == ["task_started", "task_result"]
    assert {e.data["childSessionId"] for e in task_events} == {child_id}
    assert task_events[0].data["spawnEntryId"] == spawned_by["entryId"] == spawn_leaf
    assert parent._session_mgr.get_leaf() == spawn_leaf

    shutil.rmtree(run_record.run_dir(child_id))
    status = json.loads(parent.run_status(child_id))
    assert status["status"] == "completed"
    assert status["task_id"] == rec["task_id"]
    out = json.loads(parent.run_output(child_id))
    assert out["status"] == "completed"
    assert out["taskId"] == rec["task_id"]
    assert "enveloped result" in out["summary"]


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

    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager
    child = SessionManager.open(_only_run_id(parent))
    contents = str([e.data for e in child.entries() if e.type == T.MESSAGE])
    assert "persisted body" in contents


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
    rec = json.loads(parent.run_list())[0]
    assert rec["agent_type"] == "coder"


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
    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager
    run_id = _only_run_id(parent)
    first_n = sum(1 for e in SessionManager.open(run_id).entries() if e.type == T.MESSAGE)
    assert first_n >= 2

    # resume：reload 历史，追加新 prompt
    reloaded = {}

    def _spy_build2(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            from nanocode.session import tree as _T
            # docs/14 SessionLease：resume 历史在 child 树（_build_sub_agent 已打开已存在的 child 租约），
            # 不再装进 flat 列表——从 child 树分支量历史。
            reloaded["history_len_before_run"] = sum(
                1 for e in sub._session_mgr.get_branch() if e.type == _T.MESSAGE)
            return {"text": "second", "tokens": {"input": 3, "output": 2}}

        sub.run_once = _ro
        return sub

    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build2)
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "second prompt", "resume": run_id}))
    # P3: resume also returns the bounded envelope (summary passthrough + result_path).
    assert "second" in res
    assert "result.md" in res
    # resume 不新增记录
    assert len(json.loads(parent.run_list())) == 1
    # run_once 之前历史已 reload（>=2 条）
    assert reloaded["history_len_before_run"] >= 2


def test_resume_unknown_id_errors():
    parent = _agent()
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "resume": "agent-999"}))
    assert "agent-999" in res
    assert "unknown" in res.lower() or "not found" in res.lower()
    assert json.loads(parent.run_list()) == []


def test_resume_provider_mismatch_errors(monkeypatch):
    parent = _agent()
    real_build = parent._build_sub_agent
    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub)
        return sub
    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "first"}))
    run_id = _only_run_id(parent)
    from nanocode.subagents import run_record
    run_record.update_status(run_id, model={"provider": "openai", "modelId": parent.model})
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "resume": run_id}))
    assert "provider" in res.lower()
    assert "mismatch" in res.lower() or "cannot" in res.lower()


def test_resume_model_mismatch_errors(monkeypatch):
    parent = _agent()
    real_build = parent._build_sub_agent
    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub)
        return sub
    monkeypatch.setattr(parent, "_build_sub_agent", _spy_build)
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "first"}))
    run_id = _only_run_id(parent)
    from nanocode.subagents import run_record
    run_record.update_status(
        run_id, model={"provider": parent.provider_runtime_config.name, "modelId": "some-other-model"})
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "resume": run_id}))
    assert "model" in res.lower()
    assert "mismatch" in res.lower() or "cannot" in res.lower()


# ─── run_in_background：立即返回 child-session run id ────


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
    run_id = _only_run_id(parent)
    assert run_id in res
    assert parent.task_manager.get_task(run_id) is None
    assert json.loads(parent.run_status(run_id))["child_session_id"] == run_id


def test_run_in_background_with_resume_errors():
    parent = _agent()
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p",
         "run_in_background": True, "resume": "agent-001"}))
    assert "resume" in res.lower()
    assert "run_in_background" in res.lower() or "background" in res.lower()
    assert json.loads(parent.run_list()) == []


# ─── provider runtime config 基础 ─────────────────────────────


def test_provider_runtime_config_anthropic():
    parent = _agent()
    assert parent.provider_runtime_config.name == "anthropic"


def test_provider_runtime_config_openai():
    parent = Agent(api_key="test",
                   permission_mode="bypassPermissions",
                   api_base="https://example.com/v1", session_id="osid")
    inject_test_services(parent)
    assert parent.provider_runtime_config.name == "openai"


def test_resume_rejects_in_flight_subagent():
    # review medium：resume 一个仍 running/idle 的子 agent 会对其 child session 取第二把 flock
    # （同进程第二 fd）→ SessionBusyError + 误导消息。须先 fail-closed 拒绝、给清晰提示。
    parent = _agent()
    real_build = parent._build_sub_agent
    def _spy_build(**kw):
        sub = real_build(**kw)
        sub.run_once = _stub_run_once(sub)
        return sub
    parent._build_sub_agent = _spy_build
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "first"}))
    run_id = _only_run_id(parent)
    from nanocode.subagents import run_record
    run_record.update_status(run_id, status="running")
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p", "resume": run_id}))
    assert "running" in res.lower() and "cannot resume" in res.lower()
    assert "locked by another writer" not in res          # 不再是误导的锁错误
