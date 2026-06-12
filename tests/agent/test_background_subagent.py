"""阶段 E：background subagent（agent(run_in_background=True)）。

- Task 1：_build_sub_agent(background=True) → auto-deny confirm_fn + 隔离空 confirmed_paths；
  background=False（默认）维持共享父 confirm_fn + _confirmed_paths 同一引用。
- Task 2：_spawn_background_subagent 立即返回 task_id；detached 协程完成后填
  task/subagent 终态 + result_summary + result.md + child 树持久化 + token 累加 + 回注。
- Task 3：failed（run_once 抛异常）/ cancelled（task_stop）/ timeout（timeout_ms）三态。
"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.runtime.spawn import _auto_deny_confirm
from nanocode.tools import tool_definitions, tasks_tool


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
                sub._anthropic_messages.append({"role": "user", "content": prompt})
                sub._anthropic_messages.append({"role": "assistant", "content": text})
                if sub._session_mgr is not None:          # 真实 run_once 会写 child 树——stub 对齐
                    sub._core._record_messages(sub, {"role": "user", "content": prompt})
                    sub._core._record_messages(sub, {"role": "assistant", "content": text})
                return {"text": text, "tokens": tokens}
            sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    return built


async def _wait_task_terminal(parent, task_id, tries=200, delay=0.02):
    from nanocode.tasks.models import TERMINAL_TASK_STATUSES
    for _ in range(tries):
        t = parent.task_manager.get_task(task_id)
        if t and t.status in TERMINAL_TASK_STATUSES:
            return t
        await asyncio.sleep(delay)
    return parent.task_manager.get_task(task_id)


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


def test_background_spawn_returns_task_id_immediately():
    parent = _agent()
    _spy_build_with_stub(parent)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "bg task",
             "prompt": "do it", "run_in_background": True})
        return res

    res = asyncio.run(scenario())
    assert "task-001" in res
    rec = parent.task_manager.get_task("task-001")
    assert rec is not None
    assert rec.kind == "subagent"
    assert rec.owner_agent_id == "agent-001"
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub is not None
    assert sub.task_id == "task-001"


def test_background_completes_and_fills_result_summary():
    parent = _agent()
    _spy_build_with_stub(parent, text="the bg output body")

    async def scenario():
        await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "completed"
    assert "the bg output body" in (rec.result_summary or "")
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "completed"


def test_background_sets_result_path_and_last_result_path():
    """P3: background completion sets task.result_path + subagent.last_result_path."""
    parent = _agent()
    _spy_build_with_stub(parent, text="bg result body")

    async def scenario():
        await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.result_path and rec.result_path.endswith("result.md")
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.last_result_path and sub.last_result_path.endswith("result.md")


def test_background_persists_messages_to_child_tree():
    parent = _agent()
    _spy_build_with_stub(parent, text="persisted bg body")

    async def scenario():
        await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        await _wait_task_terminal(parent, "task-001")

    asyncio.run(scenario())
    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager
    child = SessionManager.open("bgsid.agent-001")
    contents = str([e.data for e in child.entries() if e.type == T.MESSAGE])
    assert "persisted bg body" in contents


def test_background_token_accumulation():
    parent = _agent()
    _spy_build_with_stub(parent, tokens={"input": 21, "output": 9})
    before_in = parent.total_input_tokens
    before_out = parent.total_output_tokens

    async def scenario():
        await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        await _wait_task_terminal(parent, "task-001")

    asyncio.run(scenario())
    assert parent.total_input_tokens == before_in + 21
    assert parent.total_output_tokens == before_out + 9


def test_background_finished_task_injected_to_reminder():
    parent = _agent()
    _spy_build_with_stub(parent, text="reminder body text")

    async def scenario():
        await parent._execute_agent_tool(
            {"type": "coder", "description": "the bg desc",
             "prompt": "p", "run_in_background": True})
        await _wait_task_terminal(parent, "task-001")

    asyncio.run(scenario())
    from nanocode.session import tree as _T
    from nanocode.session.manager import SessionManager as _SM
    parent._session_mgr = parent._session_mgr or _SM.create("bgsub_inj")
    parent.agent_session.inject_finished_tasks()
    content = next(e.data["content"] for e in parent._session_mgr.entries()
                   if e.type == _T.CUSTOM_MESSAGE and e.data.get("customType") == "finished_tasks")
    assert "<system-reminder>" in content
    assert "task-001" in content
    assert "subagent" in content
    assert "reminder body text" in content
    assert parent.task_manager.get_task("task-001").injected is True


def test_background_with_resume_errors_and_registers_nothing():
    parent = _agent()
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "p",
         "run_in_background": True, "resume": "agent-001"}))
    assert "resume" in res.lower()
    assert "run_in_background" in res.lower() or "background" in res.lower()
    assert parent.task_manager.list_subagents() == []
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
        await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "failed"
    assert "boom in sub" in (rec.error or "")
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "failed"


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
        await parent._execute_agent_tool(
            {"type": "coder", "description": "d",
             "prompt": "p", "run_in_background": True})
        # 等后台协程真正进入 run_once 的 await 点（真实场景 spawn→stop 间必有 yield）
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await tasks_tool.task_stop(parent.task_manager, parent._background_tasks, "task-001")
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "cancelled"
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "cancelled"


def test_background_timeout_maps_task_timed_out_sub_failed():
    parent = _agent()

    def _slow(sub):
        async def _ro(prompt):
            await asyncio.sleep(60)
            return {"text": "never", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build_with_stub(parent, run_once=_slow)

    async def scenario():
        await parent._spawn_background_subagent(
            agent_type="coder", description="d", prompt="p", timeout_ms=50)
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "timed_out"
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "failed"


def test_background_construction_error_marks_records_failed_not_running():
    """Codex P1 round-3 regression: if building the background sub-agent raises
    BEFORE the run helper returns (e.g. config/build error), the detached task
    must still land terminal (failed), never leave task/sub stuck in 'running'."""
    parent = _agent()

    def _boom(**kw):
        raise RuntimeError("build blew up")

    parent._build_sub_agent = _boom

    async def scenario():
        await parent._spawn_background_subagent(
            agent_type="coder", description="d", prompt="p")
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "failed"
    assert "build blew up" in (rec.error or "")
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "failed"  # not left at 'running'


def test_background_timeout_reliable_even_if_run_once_swallows_cancel():
    """Codex P1 regression: Agent.chat() swallows CancelledError, so a naive
    asyncio.wait_for would let a timed-out background run be marked completed.
    A run_once that catches the cancel and returns a normal result must STILL be
    detected as a timeout (task=timed_out, sub=failed), not completed."""
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
        await parent._spawn_background_subagent(
            agent_type="coder", description="d", prompt="p", timeout_ms=50)
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "timed_out"   # NOT "completed"
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "failed"
