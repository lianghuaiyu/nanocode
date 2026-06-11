"""P4: concurrency caps (max_threads on background spawns) + max_depth backstop,
plus settings [agents] timeout fallbacks wired into _execute_agent_tool."""

from __future__ import annotations

import asyncio
import json

import pytest

from nanocode.agent.engine import Agent
from nanocode.tools import permissions
from nanocode.paths import data_dir


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="capsid", **kw)


def _set_agents_settings(monkeypatch, tmp_path, obj):
    monkeypatch.chdir(tmp_path)
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"agents": obj}))
    permissions.reset_permission_cache()


def _stub_running_bg_subagent(parent):
    """Register a running background subagent (record + task + a fake asyncio task in
    _background_tasks) so _running_background_subagent_count() sees it as live."""
    sub_rec = parent.task_manager.create_subagent(type="coder", description="bg")
    task_rec = parent.task_manager.create_task("subagent", "bg", owner_agent_id=sub_rec.id)
    parent.task_manager.update_task(task_rec.id, status="running")

    async def _never():
        await asyncio.sleep(3600)

    t = asyncio.ensure_future(_never())
    t._nanocode_task_id = task_rec.id
    parent._background_tasks.add(t)
    return t


# ─── max_threads: background spawn cap ───────────────────────────


def test_max_threads_exceeded_returns_error_and_does_not_spawn(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 1})

    async def scenario():
        parent = _agent()
        t = _stub_running_bg_subagent(parent)  # one already running -> at cap (1)
        before = len(parent._background_tasks)
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        after = len(parent._background_tasks)
        t.cancel()
        return res, before, after

    res, before, after = asyncio.run(scenario())
    assert "max concurrent sub-agents" in res.lower()
    assert "1" in res
    # no new background task was spawned
    assert after == before


def test_under_max_threads_spawns(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 3})

    spawned = {}

    async def scenario():
        parent = _agent()

        async def _fake_spawn(*, agent_type, description, prompt, timeout_ms=None):
            spawned["called"] = True
            return "task-999"

        parent._spawn_background_subagent = _fake_spawn
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        return res

    res = asyncio.run(scenario())
    assert spawned.get("called") is True
    assert "task-999" in res


def test_running_count_only_counts_running_subagent_tasks(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 5})

    async def scenario():
        parent = _agent()
        t1 = _stub_running_bg_subagent(parent)
        t2 = _stub_running_bg_subagent(parent)
        # mark one completed -> should drop out of the live count
        tid = t2._nanocode_task_id
        parent.task_manager.update_task(tid, status="completed")
        n = parent._running_background_subagent_count()
        t1.cancel(); t2.cancel()
        return n

    n = asyncio.run(scenario())
    assert n == 1


# ─── max_depth backstop ──────────────────────────────────────────


def test_max_depth_backstop_blocks_spawn(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_depth": 1})
    # A sub-agent at depth=1 trying to spawn (depth+1=2) > max_depth(1) -> blocked.
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="s", tools=[], agent_type="coder")
    assert sub.depth == 1
    res = asyncio.run(sub._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert "max sub-agent depth" in res.lower()
    assert "1" in res


def test_main_agent_depth0_not_blocked_when_depth_allows(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_depth": 1})

    captured = {}

    async def scenario():
        parent = _agent()  # depth 0 -> spawn depth 1 <= max_depth 1 -> allowed

        def _spy(**kw):
            captured["built"] = True
            real = Agent._build_sub_agent(parent, **kw)

            async def _ro(prompt):
                return {"text": "ok", "tokens": {"input": 0, "output": 0}}

            real.run_once = _ro
            return real

        parent._build_sub_agent = _spy
        return await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p"})

    res = asyncio.run(scenario())
    assert "max sub-agent depth" not in res.lower()
    assert captured.get("built") is True


def test_max_depth_zero_disables_cap(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_depth": 0})
    parent = _agent()
    sub = parent._build_sub_agent(system_prompt="s", tools=[], agent_type="coder")
    # max_depth=0 disables the cap -> never blocks on depth.
    assert sub._depth_cap_exceeded() is False


# ─── settings timeout fallbacks wired in ─────────────────────────


def test_foreground_default_timeout_from_settings(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"default_timeout_ms": 40})

    parent = _agent()

    def _spy(**kw):
        real = Agent._build_sub_agent(parent, **kw)

        async def _slow(prompt):
            await asyncio.sleep(60)

        real.run_once = _slow
        return real

    parent._build_sub_agent = _spy
    # no timeout_ms in tool call, no manifest timeout -> settings default 40ms applies
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert "timed out" in res.lower()
    assert "40" in res


def test_background_timeout_from_settings(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"background_timeout_ms": 77})

    captured = {}

    async def scenario():
        parent = _agent()

        async def _fake_spawn(*, agent_type, description, prompt, timeout_ms=None):
            captured["timeout_ms"] = timeout_ms
            return "task-1"

        parent._spawn_background_subagent = _fake_spawn
        await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})

    asyncio.run(scenario())
    assert captured["timeout_ms"] == 77


def test_tool_timeout_beats_settings(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"default_timeout_ms": 9999})

    parent = _agent()

    def _spy(**kw):
        real = Agent._build_sub_agent(parent, **kw)

        async def _slow(prompt):
            await asyncio.sleep(60)

        real.run_once = _slow
        return real

    parent._build_sub_agent = _spy
    # explicit tool timeout_ms=30 wins over settings default 9999
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p", "timeout_ms": 30}))
    assert "timed out" in res.lower()
    assert "30" in res


# ─── Codex P4 regressions: curators count toward max_threads ─────


def _stub_running_curator(parent):
    """A running background curator (kind='memory_consolidate') with owner_agent_id —
    it runs a real sub-agent and must count toward max_threads."""
    sub_rec = parent.task_manager.create_subagent(type="memory-curator", description="curate")
    task_rec = parent.task_manager.create_task(
        "memory_consolidate", "curate", owner_agent_id=sub_rec.id)
    parent.task_manager.update_task(task_rec.id, status="running")

    async def _never():
        await asyncio.sleep(3600)

    t = asyncio.ensure_future(_never())
    t._nanocode_task_id = task_rec.id
    parent._background_tasks.add(t)
    return t


def test_running_count_includes_curator_subagents(monkeypatch, tmp_path):
    """Codex P4 MED: memory curator/eval background sub-agents (kind != 'subagent')
    were bypassing max_threads. The count keys on owner_agent_id, so they count;
    a plain background shell task (no owner) does not."""
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 5})

    async def scenario():
        parent = _agent()
        t1 = _stub_running_bg_subagent(parent)   # kind=subagent
        t2 = _stub_running_curator(parent)       # kind=memory_consolidate
        # a background SHELL task has no owner_agent_id -> must NOT count
        sh = parent.task_manager.create_task("shell", "echo", owner_agent_id=None)
        parent.task_manager.update_task(sh.id, status="running")
        n = parent._running_background_subagent_count()
        t1.cancel(); t2.cancel()
        return n

    n = asyncio.run(scenario())
    assert n == 2   # subagent + curator, NOT the shell task


# ─── Codex P4 regression: skill-fork honors max_depth ────────────


def test_skill_fork_blocked_for_subagents(monkeypatch, tmp_path):
    """Holistic review (Codex): a sub-agent must not spawn descendants by ANY meta
    route. fork-mode skills are refused for sub-agents outright (stronger than the
    depth cap, which still bounds the main agent)."""
    def _fake_get_skill(name):
        return None

    def _fake_execute(name, args):
        return {"context": "fork", "prompt": "do it", "allowed_tools": []}

    monkeypatch.setattr("nanocode.skills.get_skill_by_name", _fake_get_skill, raising=False)
    monkeypatch.setattr("nanocode.skills.execute_skill", _fake_execute, raising=False)

    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="s", tools=parent.tools, agent_type="coder")
    assert sub.depth == 1
    res = asyncio.run(sub._execute_skill_tool({"skill_name": "s", "args": "a"}))
    assert "not available to sub-agents" in res.lower()
    # no skill-fork sub-agent record was created
    assert [s for s in sub.task_manager.list_subagents() if s.type == "skill-fork"] == []
    # the MAIN agent can still fork (subject to depth cap)
    main_res = asyncio.run(parent._execute_skill_tool({"skill_name": "s", "args": "a"}))
    assert "not available to sub-agents" not in main_res.lower()


# ─── Holistic review MED: curators HONOR max_threads (not just count) ──


def test_memory_consolidate_respects_max_threads(monkeypatch, tmp_path):
    """Curators counted toward max_threads but never checked it (counted-but-not-capped).
    A consolidate spawn must be refused when the cap is already reached."""
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 1})
    monkeypatch.setattr("nanocode.agent.engine.build_curator_user_message",
                        lambda: "memory: some content to consolidate", raising=False)

    async def scenario():
        parent = _agent()
        t1 = _stub_running_bg_subagent(parent)   # fills the single slot
        res = await parent._spawn_memory_consolidate()
        # no new curator subagent record was created
        curators = [s for s in parent.task_manager.list_subagents()
                    if s.type == "memory-curator"]
        t1.cancel()
        return res, curators

    res, curators = asyncio.run(scenario())
    assert "max concurrent sub-agents" in res.lower()
    assert curators == []
