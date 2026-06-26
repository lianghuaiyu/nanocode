"""P4: concurrency caps (max_threads on background spawns) + max_depth backstop,
plus settings [agents] timeout fallbacks wired into _execute_agent_tool."""

from __future__ import annotations

import asyncio
import json

import pytest

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager
from nanocode.subagents import run_record
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
    """Register a running background subagent run record and a fake live task."""
    if parent._session_mgr is None:
        parent._session_mgr = SessionManager.create(parent.session_id)
    run_id = T.new_id("sess")
    child = SessionManager.create(
        run_id,
        parent_session={"sessionId": parent.session_id, "entryId": None,
                        "taskId": run_id, "agentId": run_id},
    )
    child.close()
    run_record.create_run_record(
        child_session_id=run_id,
        parent_session_id=parent.session_id,
        spawn_entry_id=None,
        tool_call_id=None,
        agent_type="coder",
        description="capability run",
        background=True,
        context_mode="fresh",
        isolation="shared",
        worktree_path=None,
        model={"provider": parent._current_provider(), "modelId": parent.model},
        prompt="bg",
    )

    async def _never():
        await asyncio.sleep(3600)

    t = asyncio.ensure_future(_never())
    t._nanocode_run_id = run_id
    parent._background_tasks.add(t)
    return t


# ─── max_threads: background spawn cap ───────────────────────────


def test_max_threads_exceeded_queues_background_run(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 1})

    async def scenario():
        parent = _agent()
        t = _stub_running_bg_subagent(parent)  # one already running -> at cap (1)
        before = len(parent._background_tasks)
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        after = len(parent._background_tasks)
        run_id = res.split("run ", 1)[1].split(".", 1)[0]
        status = run_record.read_status(run_id)["status"]
        t.cancel()
        return res, before, after, status

    res, before, after, status = asyncio.run(scenario())
    assert "background sub-agent run" in res.lower()
    assert status == "queued"
    assert after == before + 1


def test_under_max_threads_spawns(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 3})

    spawned = {}

    async def scenario():
        parent = _agent()

        async def _fake_spawn(*, agent_type, description, prompt, timeout_ms=None, **_kw):
            spawned["called"] = True
            return "sess_fake"

        parent._spawn_background_subagent = _fake_spawn
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        return res

    res = asyncio.run(scenario())
    assert spawned.get("called") is True
    assert "sess_fake" in res


def test_running_count_only_counts_running_subagent_tasks(monkeypatch, tmp_path):
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 5})

    async def scenario():
        parent = _agent()
        t1 = _stub_running_bg_subagent(parent)
        t2 = _stub_running_bg_subagent(parent)
        # mark one completed -> should drop out of the live count
        run_record.complete_run(
            t2._nanocode_run_id, status="completed", result="done",
            result_entry_id=None, prompt_entry_id=None)
        n = parent._subagents.running_background_count()
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
    assert sub._subagents.depth_cap_exceeded() is False


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

        async def _fake_spawn(*, agent_type, description, prompt, timeout_ms=None, **_kw):
            captured["timeout_ms"] = timeout_ms
            return "sess_fake"

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
    """A running background curator (agent_type='memory-curator') as a child-session
    run record (docs/25 A2 single ledger). It counts toward max_threads via
    ``_nanocode_run_id`` — like any background sub-agent — not via a host task."""
    if parent._session_mgr is None:
        parent._session_mgr = SessionManager.create(parent.session_id)
    run_id = T.new_id("sess")
    child = SessionManager.create(
        run_id,
        parent_session={"sessionId": parent.session_id, "entryId": None,
                        "taskId": run_id, "agentId": run_id},
    )
    child.close()
    run_record.create_run_record(
        child_session_id=run_id,
        parent_session_id=parent.session_id,
        spawn_entry_id=None,
        tool_call_id=None,
        agent_type="memory-curator",
        description="curate",
        background=True,
        context_mode="fresh",
        isolation="shared",
        worktree_path=None,
        model={"provider": parent._current_provider(), "modelId": parent.model},
        prompt="curate",
        inject_summary=True,
    )

    async def _never():
        await asyncio.sleep(3600)

    t = asyncio.ensure_future(_never())
    t._nanocode_run_id = run_id
    parent._background_tasks.add(t)
    return t


def test_running_count_includes_curator_subagents(monkeypatch, tmp_path):
    """memory curator/eval background sub-agents must count toward max_threads. After
    docs/25 A2 they are child-session run records (single ledger), counted via
    ``_nanocode_run_id``; a plain background shell task (no run id) does not count."""
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 5})

    async def scenario():
        parent = _agent()
        t1 = _stub_running_bg_subagent(parent)   # agent_type=coder
        t2 = _stub_running_curator(parent)       # agent_type=memory-curator
        # a background SHELL task has no run id -> must NOT count
        sh = parent.task_manager.create_task("shell", "echo", owner_agent_id=None)
        parent.task_manager.update_task(sh.id, status="running")
        n = parent._subagents.running_background_count()
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
    # no skill-fork run record was created
    assert [s for s in json.loads(parent.run_list()) if s["agent_type"] == "skill-fork"] == []
    # the MAIN agent can still fork (subject to depth cap)
    main_res = asyncio.run(parent._execute_skill_tool({"skill_name": "s", "args": "a"}))
    assert "not available to sub-agents" not in main_res.lower()


# ─── Holistic review MED: curators HONOR max_threads (not just count) ──


def test_memory_consolidate_respects_max_threads(monkeypatch, tmp_path):
    """Curators counted toward max_threads but never checked it (counted-but-not-capped).
    A consolidate spawn must be refused when the cap is already reached."""
    _set_agents_settings(monkeypatch, tmp_path, {"max_threads": 1})
    # docs/15 Phase 6：memory curator spawn 已搬到 runtime/spawn.py,build_curator_user_message
    # 在该模块被调用 → monkeypatch 目标随之迁移。
    monkeypatch.setattr("nanocode.runtime.spawn.build_curator_user_message",
                        lambda: "memory: some content to consolidate", raising=False)

    async def scenario():
        parent = _agent()
        t1 = _stub_running_bg_subagent(parent)   # fills the single slot
        res = await parent._spawn_memory_consolidate()
        # no new curator run record was created
        curators = [s for s in json.loads(parent.run_list())
                    if s["agent_type"] == "memory-curator"]
        t1.cancel()
        return res, curators

    res, curators = asyncio.run(scenario())
    assert "max concurrent sub-agents" in res.lower()
    assert curators == []
