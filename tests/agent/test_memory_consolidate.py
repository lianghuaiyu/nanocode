"""Task 2: 记忆巩固线 _spawn_memory_consolidate + _run_memory_consolidate。

判断型由 curator 子 agent（无工具，只出 JSON 提案）；确定性 parse+apply 由宿主 Python
（_run_memory_consolidate 内子 agent 完成后跑）。绕开 _execute_agent_tool（其 type 归一会把
memory-curator 改成 coder 拿全工具），直接 get_sub_agent_config + _build_sub_agent(background=True)。

测试不跑真 API：spy _build_sub_agent 注入 stub run_once 返回固定 JSON；apply_plan 在隔离
NANOCODE_HOME 真跑（先 seed 几个 .md 到 project_memory_dir）。
"""

import asyncio
import json

import pytest

from nanocode.agent.engine import Agent
from nanocode.paths import project_memory_dir
from nanocode.subagents.prompts import MEMORY_CURATOR_TYPE


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="memsid", **kw)


def _seed_memories():
    """在隔离 NANOCODE_HOME 下种几个 .md（project_memory_dir 受 conftest NANOCODE_HOME 隔离）。"""
    mem = project_memory_dir()
    (mem / "project_goals.md").write_text(
        "---\nname: goals\ndescription: project goals\ntype: project\n---\n"
        "We want to ship v2 by end of Q1."
    )
    (mem / "stale_note.md").write_text(
        "---\nname: stale\ndescription: obsolete note\ntype: feedback\n---\n"
        "Old TODO that is no longer relevant."
    )
    return mem


def _spy_build_with_stub(parent, *, run_once=None, text=None, tokens=None, captured=None):
    """spy _build_sub_agent：注入 stub run_once。记录构造 kwargs 进 captured。"""
    tokens = tokens or {"input": 11, "output": 7}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)
        if captured is not None:
            captured["kw"] = kw
            captured["sub"] = sub
        if run_once is not None:
            sub.run_once = run_once(sub)
        else:
            async def _ro(prompt: str) -> dict:
                sub._anthropic_messages.append({"role": "user", "content": prompt})
                sub._anthropic_messages.append({"role": "assistant", "content": text})
                return {"text": text, "tokens": tokens}
            sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


async def _wait_task_terminal(parent, task_id, tries=200, delay=0.02):
    from nanocode.tasks.models import TERMINAL_TASK_STATUSES
    for _ in range(tries):
        t = parent.task_manager.get_task(task_id)
        if t and t.status in TERMINAL_TASK_STATUSES:
            return t
        await asyncio.sleep(delay)
    return parent.task_manager.get_task(task_id)


_DELETE_PLAN = json.dumps({
    "summary": "Archive the stale note and refresh goals",
    "actions": [
        {"action": "delete", "targets": ["stale_note.md"], "reason": "obsolete"},
        {"action": "rewrite", "targets": ["project_goals.md"],
         "new_content": "---\nname: goals\ntype: project\n---\nShip v3 by Q2.",
         "reason": "updated goal"},
    ],
})

_EMPTY_PLAN = json.dumps({"summary": "No cleanup needed", "actions": []})


# ─── 1. applies_plan_and_sets_summary ────────────────────────


def test_applies_plan_and_sets_summary():
    parent = _agent()
    _seed_memories()
    captured = {}
    _spy_build_with_stub(parent, text=_DELETE_PLAN, captured=captured)

    async def scenario():
        res = await parent._spawn_memory_consolidate()
        task_id = res.split()[-1] if "task" not in res else "task-001"
        return await _wait_task_terminal(parent, "task-001"), res

    rec, res = asyncio.run(scenario())
    # 子 agent 用 memory-curator 类型 + background=True
    assert captured["kw"]["agent_type"] == MEMORY_CURATOR_TYPE
    assert captured["kw"]["background"] is True
    # task kind=memory_consolidate, completed, summary 含 archived/rewritten/backup=
    assert rec.kind == "memory_consolidate"
    assert rec.status == "completed"
    summary = rec.result_summary or ""
    assert "archived" in summary
    assert "rewritten" in summary
    assert "backup=" in summary
    # 真落库：stale 归档（原文件不在），goals 改写
    mem = project_memory_dir()
    assert not (mem / "stale_note.md").exists()
    assert "Ship v3 by Q2" in (mem / "project_goals.md").read_text()
    # subagent completed
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "completed"


# ─── 2. archives_not_hard_delete ─────────────────────────────


def test_archives_not_hard_delete():
    parent = _agent()
    mem = _seed_memories()
    _spy_build_with_stub(parent, text=_DELETE_PLAN)

    async def scenario():
        await parent._spawn_memory_consolidate()
        await _wait_task_terminal(parent, "task-001")

    asyncio.run(scenario())
    from nanocode.memory.maintenance import _archive_dir
    archive = _archive_dir()
    archived = [f for f in archive.iterdir() if "stale_note" in f.name and not f.name.endswith(".meta.json")]
    assert len(archived) == 1


# ─── 3. bad_json_no_apply_completed ──────────────────────────


def test_bad_json_no_apply_completed():
    parent = _agent()
    mem = _seed_memories()
    _spy_build_with_stub(parent, text="this is not json at all {")

    async def scenario():
        await parent._spawn_memory_consolidate()
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "completed"
    assert "no changes" in (rec.result_summary or "").lower()
    # 记忆未动
    assert (mem / "stale_note.md").exists()
    assert "ship v2 by end of Q1" in (mem / "project_goals.md").read_text()


# ─── 4. empty_plan_completed_no_changes ──────────────────────


def test_empty_plan_completed_no_changes():
    parent = _agent()
    mem = _seed_memories()
    _spy_build_with_stub(parent, text=_EMPTY_PLAN)

    async def scenario():
        await parent._spawn_memory_consolidate()
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "completed"
    assert "no changes" in (rec.result_summary or "").lower()
    # 记忆未动
    assert (mem / "stale_note.md").exists()


# ─── 5. no_memories_skips_no_task ────────────────────────────


def test_no_memories_skips_no_task():
    parent = _agent()
    # 不 seed：project_memory_dir 为空
    project_memory_dir()
    res = asyncio.run(parent._spawn_memory_consolidate())
    assert "No memories to consolidate" in res
    # 不建 task / subagent
    assert parent.task_manager.list_tasks() == []
    assert parent.task_manager.list_subagents() == []


# ─── 6. finished_task_injected_with_summary ──────────────────


def test_finished_task_injected_with_summary():
    parent = _agent()
    _seed_memories()
    _spy_build_with_stub(parent, text=_DELETE_PLAN)

    async def scenario():
        await parent._spawn_memory_consolidate()
        await _wait_task_terminal(parent, "task-001")

    asyncio.run(scenario())
    from nanocode.session import tree as _T
    from nanocode.session.manager import SessionManager as _SM
    parent._session_mgr = parent._session_mgr or _SM.create("memcon_inj")
    parent.agent_session.inject_finished_tasks()
    content = next(e.data["content"] for e in parent._session_mgr.entries()
                   if e.type == _T.CUSTOM_MESSAGE and e.data.get("customType") == "finished_tasks")
    assert "<system-reminder>" in content
    assert "task-001" in content
    assert "memory_consolidate" in content
    assert "archived" in content
    assert parent.task_manager.get_task("task-001").injected is True


# ─── 7. curator_error_marks_failed ───────────────────────────


def test_curator_error_marks_failed():
    parent = _agent()
    _seed_memories()

    def _raising(sub):
        async def _ro(prompt):
            raise RuntimeError("curator boom")
        return _ro

    _spy_build_with_stub(parent, run_once=_raising)

    async def scenario():
        await parent._spawn_memory_consolidate()
        return await _wait_task_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "failed"
    assert "curator boom" in (rec.error or "")
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.status == "failed"


# ─── 8. memory_tool_consolidate_action_delegates ─────────────


def test_memory_tool_consolidate_action_delegates():
    parent = _agent()
    _seed_memories()
    _spy_build_with_stub(parent, text=_DELETE_PLAN)

    async def scenario():
        res = await parent._execute_tool_call("memory", {"action": "consolidate"})
        await _wait_task_terminal(parent, "task-001")
        return res

    res = asyncio.run(scenario())
    assert "task-001" in res
    rec = parent.task_manager.get_task("task-001")
    assert rec is not None
    assert rec.kind == "memory_consolidate"
