"""阶段 E：background subagent（agent(run_in_background=True)）。"""

import asyncio
import json
import re

import pytest

from nanocode.agent.engine import Agent
from nanocode.agent.events import ToolCallRequested, ToolResultObserved
from nanocode.runs.models import TERMINAL_RUN_STATUSES
from nanocode.runtime.spawn import _auto_deny_confirm
from nanocode.subagents import run_record
from nanocode.tools import REGISTRY

tool_definitions = REGISTRY.schemas()


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="bgsid", **kw)


def _spy_build_with_stub(parent, *, text="bg done", tokens=None, run_once=None):
    """spy _build_sub_agent：注入 stub run_once，写入 messages 历史。返回 built dict。"""
    tokens = tokens or {"input": 13, "output": 5}
    built = {}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)
        built["sub"] = sub
        built["kw"] = kw
        if run_once is not None:
            sub.run_once = run_once(sub)
        else:
            async def _ro(prompt: str) -> dict:
                if sub._session_mgr is not None:          # 真实 run_once 会写 child 树——stub 对齐
                    sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
                    sub.agent_session.record_provider_messages({"role": "assistant", "content": text})
                return {"text": text, "tokens": tokens}
            sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    return built


async def _wait_run_terminal(run_id, tries=200, delay=0.02):
    for _ in range(tries):
        try:
            status = run_record.read_status(run_id)
        except FileNotFoundError:
            status = None
        if status and status["status"] in TERMINAL_RUN_STATUSES:
            return status
        await asyncio.sleep(delay)
    return run_record.read_status(run_id)


def _run_id_from_started(text: str) -> str:
    m = re.search(r"run (sess_[A-Za-z0-9_]+)", text)
    assert m, text
    return m.group(1)


# ─── Task 1：_build_sub_agent(background=...) ────────────────


def test_auto_deny_confirm_returns_false():
    assert asyncio.run(_auto_deny_confirm("rm -rf /")) is False


def test_background_sub_uses_auto_deny_confirm():
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder",
        background=True)
    assert sub.confirm_fn is _auto_deny_confirm
    assert asyncio.run(sub.confirm_fn("anything")) is False


def test_background_sub_has_isolated_empty_confirmed_paths():
    parent = _agent(confirmed_paths={"/parent/confirmed"})
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder",
        background=True)
    assert sub._confirmed_paths == set()
    assert sub._confirmed_paths is not parent._confirmed_paths
    assert "/parent/confirmed" not in sub._confirmed_paths
    # 隔离：子加入的路径不回流到父
    sub._confirmed_paths.add("/child/x")
    assert "/child/x" not in parent._confirmed_paths


def test_background_sub_inherits_mode_and_shares_manager():
    parent = _agent(permission_mode="default")
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder",
        background=True)
    assert sub.permission_mode == "default"
    assert sub.task_manager is parent.task_manager
    assert sub.session_id == parent.session_id
    assert sub.is_sub_agent is True
    assert all(t["name"] != "agent" for t in sub.tools)


def test_foreground_sub_still_shares_confirm_and_paths():
    async def cf(_cmd):
        return True

    shared = {"/already/confirmed"}
    parent = _agent(confirm_fn=cf, confirmed_paths=shared)
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder")
    assert sub.confirm_fn is parent.confirm_fn is cf
    assert sub._confirmed_paths is parent._confirmed_paths is shared


# ─── Task 2：_spawn_background_subagent + _run_background_subagent ──


def test_background_spawn_returns_run_id_immediately():
    parent = _agent()
    _spy_build_with_stub(parent)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "bg task",
             "prompt": "do it", "run_in_background": True})
        run_id = _run_id_from_started(res)
        return res, run_record.read_status(run_id)

    res, status = asyncio.run(scenario())
    run_id = _run_id_from_started(res)
    assert parent.task_manager.get_task(run_id) is None
    assert status["runId"] == run_id
    assert status["background"] is True


def test_background_completes_and_fills_run_record_result():
    parent = _agent()
    _spy_build_with_stub(parent, text="the bg output body")

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        run_id = _run_id_from_started(res)
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "completed"
    assert "the bg output body" in run_record.read_result(run_id)


def test_background_projects_child_tool_activity_to_run_record():
    parent = _agent()

    def _run_once(sub):
        async def _ro(prompt: str) -> dict:
            if sub._session_mgr is not None:
                sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
            sub.emit(ToolCallRequested(
                tool="read_file",
                input={"file_path": "src/nanocode/subagents/run_record.py"},
                tool_use_id="tu_read",
            ))
            sub.emit(ToolResultObserved(
                tool="read_file",
                tool_use_id="tu_read",
                chars=9,
                result="file body",
            ))
            if sub._session_mgr is not None:
                sub.agent_session.record_provider_messages({"role": "assistant", "content": "done"})
            return {"text": "done", "tokens": {"input": 3, "output": 2}}
        return _ro

    _spy_build_with_stub(parent, run_once=_run_once)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        run_id = _run_id_from_started(res)
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "completed"
    metrics = status["metrics"]
    assert metrics["toolUses"] == 1
    assert metrics["activeTools"] == []
    assert metrics["currentTool"] is None
    assert metrics["turnCount"] == 0
    events = run_record.read_events(run_id)
    assert any(e["type"] == "tool_started" and e["tool"] == "read_file" for e in events)
    assert any(e["type"] == "tool_finished" and e["toolUseId"] == "tu_read" for e in events)


def test_background_sets_run_record_result_path():
    """Background completion sets child-owned run record resultPath."""
    parent = _agent()
    _spy_build_with_stub(parent, text="bg result body")

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        run_id = _run_id_from_started(res)
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["resultPath"] and status["resultPath"].endswith("result.md")


def test_background_persists_messages_to_child_tree():
    parent = _agent()
    _spy_build_with_stub(parent, text="persisted bg body")

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        run_id = _run_id_from_started(res)
        await _wait_run_terminal(run_id)
        return run_id

    run_id = asyncio.run(scenario())
    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager
    child = SessionManager.open(run_id)
    contents = str([e.data for e in child.entries() if e.type == T.MESSAGE])
    assert "persisted bg body" in contents


def test_background_token_accumulation():
    parent = _agent()
    _spy_build_with_stub(parent, tokens={"input": 21, "output": 9})
    before_in = parent.total_input_tokens
    before_out = parent.total_output_tokens

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        await _wait_run_terminal(_run_id_from_started(res))

    asyncio.run(scenario())
    assert parent.total_input_tokens == before_in + 21
    assert parent.total_output_tokens == before_out + 9


def test_background_result_query_reads_run_record():
    parent = _agent()
    _spy_build_with_stub(parent, text="reminder body text")

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "the bg desc",
             "prompt": "p", "run_in_background": True})
        run_id = _run_id_from_started(res)
        await _wait_run_terminal(run_id)
        return run_id

    run_id = asyncio.run(scenario())
    out = json.loads(parent.run_output(run_id))
    assert out["childSessionId"] == run_id
    assert out["status"] == "completed"
    assert "reminder body text" in out["result"]
    assert parent.task_manager.get_task(run_id) is None


def test_background_with_resume_errors_and_registers_nothing():
    parent = _agent()
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p",
         "run_in_background": True, "resume": "agent-001"}))
    assert "resume" in res.lower()
    assert "run_in_background" in res.lower() or "background" in res.lower()
    assert json.loads(parent.run_list()) == []
    assert parent.task_manager.list_tasks() == []


# ─── Task 3：failed / cancelled / timeout ────────────────────


def test_background_failed_when_run_once_raises():
    parent = _agent()

    def _raising(sub):
        async def _ro(prompt):
            raise RuntimeError("boom in sub")
        return _ro

    _spy_build_with_stub(parent, run_once=_raising)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        run_id = _run_id_from_started(res)
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "failed"
    assert "boom in sub" in (status["error"] or "")


def test_background_cancelled_via_task_stop():
    parent = _agent()
    started = asyncio.Event()

    def _slow(sub):
        async def _ro(prompt):
            started.set()
            await asyncio.sleep(60)
            return {"text": "never", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build_with_stub(parent, run_once=_slow)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        run_id = _run_id_from_started(res)
        # 等后台协程真正进入 run_once 的 await 点（真实场景 spawn→stop 间必有 yield）
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await parent.run_cancel(run_id)
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "cancelled"


def test_background_timeout_marks_run_timed_out():
    parent = _agent()

    def _slow(sub):
        async def _ro(prompt):
            await asyncio.sleep(60)
            return {"text": "never", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build_with_stub(parent, run_once=_slow)

    async def scenario():
        run_id = await parent._spawn_background_subagent(
            agent_type="coder", description="d", prompt="p", timeout_ms=50)
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "timed_out"


def test_background_construction_error_marks_records_failed_not_running():
    """Codex P1 round-3 regression: if building the background sub-agent raises
    BEFORE the run helper returns (e.g. config/build error), the detached task
    must still land terminal (failed), never leave task/sub stuck in 'running'."""
    parent = _agent()

    def _boom(**kw):
        raise RuntimeError("build blew up")

    parent._build_sub_agent = _boom

    async def scenario():
        run_id = await parent._spawn_background_subagent(
            agent_type="coder", description="d", prompt="p")
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "failed"
    assert "build blew up" in (status["error"] or "")


def test_background_timeout_reliable_even_if_run_once_swallows_cancel():
    """Codex P1 regression: Agent.chat() swallows CancelledError, so a naive
    asyncio.wait_for would let a timed-out background run be marked completed.
    A run_once that catches the cancel and returns a normal result must STILL be
        detected as a timeout, not completed."""
    parent = _agent()

    def _swallows_cancel(sub):
        async def _ro(prompt):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # mimic Agent.chat(): swallow the cancel and return normally
                sub._aborted = True
                return {"text": "partial-after-cancel", "tokens": {"input": 1, "output": 1}}
            return {"text": "never", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build_with_stub(parent, run_once=_swallows_cancel)

    async def scenario():
        run_id = await parent._spawn_background_subagent(
            agent_type="coder", description="d", prompt="p", timeout_ms=50)
        return run_id, await _wait_run_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "timed_out"   # NOT "completed"
