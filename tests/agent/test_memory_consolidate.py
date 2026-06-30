"""Task 2: 记忆巩固线 _spawn_memory_consolidate + _run_memory_consolidate。

判断型由 curator 子 agent（无工具，只出 JSON 提案）；确定性 parse+apply 由宿主 Python
（_run_memory_consolidate 内子 agent 完成后跑）。绕开 _execute_agent_tool（其 type 归一会把
memory-curator 改成 coder 拿全工具），直接 get_sub_agent_config + _build_sub_agent(background=True)。

docs/25 A2：单账本 = child-session run_record（不再镜像 host TaskManager）；完成摘要经
inject_summary=True 由 FinishedTasksProvider PUSH 回父上下文。

测试不跑真 API：spy _build_sub_agent 注入 stub run_once 返回固定 JSON；apply_plan 在隔离
NANOCODE_HOME 真跑（先 seed 几个 .md 到 project_memory_dir）。
"""

import asyncio
import json
import re

from nanocode.agent.engine import Agent
from nanocode.memory.service import MemoryService, MemoryServiceConfig
from nanocode.paths import project_memory_dir
from nanocode.runs.models import TERMINAL_RUN_STATUSES
from nanocode.subagents import run_record
from nanocode.subagents.prompts import MEMORY_CURATOR_TYPE
from .._helpers import inject_test_services


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    _injected_agent = Agent(api_key="test", session_id="memsid", **kw)
    inject_test_services(_injected_agent)
    return _injected_agent


def _markdown_memory_service():
    return MemoryService(
        config=MemoryServiceConfig(backend="markdown"),
        cwd=".",
        agent_dir=".",
    )


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
                return {"text": text, "tokens": tokens}
            sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


def _run_id(res: str) -> str:
    """从 spawn 返回串里抽 child-session run id（"...run sess_xxx."）。"""
    m = re.search(r"run (sess_[A-Za-z0-9_]+)", res)
    assert m, f"no run id in: {res!r}"
    return m.group(1)


async def _wait_run_terminal(parent, run_id, tries=200, delay=0.02):
    for _ in range(tries):
        try:
            st = run_record.read_status(run_id)
        except FileNotFoundError:
            st = None
        if st and st["status"] in TERMINAL_RUN_STATUSES:
            return st
        await asyncio.sleep(delay)
    return run_record.read_status(run_id)


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
        run_id = _run_id(res)
        return await _wait_run_terminal(parent, run_id), run_id

    st, run_id = asyncio.run(scenario())
    # 子 agent 用 memory-curator 类型 + background=True
    assert captured["kw"]["agent_type"] == MEMORY_CURATOR_TYPE
    assert captured["kw"]["background"] is True
    # run_record completed，summary 含 archived/rewritten/backup=
    assert st["status"] == "completed"
    assert st["agentType"] == MEMORY_CURATOR_TYPE
    summary = st.get("resultSummary") or ""
    assert "archived" in summary
    assert "rewritten" in summary
    assert "backup=" in summary
    assert st["injectSummary"] is True
    # 真落库：stale 归档（原文件不在），goals 改写
    mem = project_memory_dir()
    assert not (mem / "stale_note.md").exists()
    assert "Ship v3 by Q2" in (mem / "project_goals.md").read_text()
    # 单账本：不再镜像 host TaskManager
    assert run_id.startswith("sess_")
    assert parent.task_manager.list_tasks() == []


# ─── 2. archives_not_hard_delete ─────────────────────────────


def test_archives_not_hard_delete():
    parent = _agent()
    mem = _seed_memories()
    _spy_build_with_stub(parent, text=_DELETE_PLAN)

    async def scenario():
        res = await parent._spawn_memory_consolidate()
        await _wait_run_terminal(parent, _run_id(res))

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
        res = await parent._spawn_memory_consolidate()
        return await _wait_run_terminal(parent, _run_id(res))

    st = asyncio.run(scenario())
    assert st["status"] == "completed"
    assert "no changes" in (st.get("resultSummary") or "").lower()
    # 记忆未动
    assert (mem / "stale_note.md").exists()
    assert "ship v2 by end of Q1" in (mem / "project_goals.md").read_text()


# ─── 4. empty_plan_completed_no_changes ──────────────────────


def test_empty_plan_completed_no_changes():
    parent = _agent()
    mem = _seed_memories()
    _spy_build_with_stub(parent, text=_EMPTY_PLAN)

    async def scenario():
        res = await parent._spawn_memory_consolidate()
        return await _wait_run_terminal(parent, _run_id(res))

    st = asyncio.run(scenario())
    assert st["status"] == "completed"
    assert "no changes" in (st.get("resultSummary") or "").lower()
    # 记忆未动
    assert (mem / "stale_note.md").exists()


# ─── 5. no_memories_skips_no_task ────────────────────────────


def test_no_memories_skips_no_task():
    parent = _agent()
    # 不 seed：project_memory_dir 为空
    project_memory_dir()
    res = asyncio.run(parent._spawn_memory_consolidate())
    assert "No memories to consolidate" in res
    # 不建 task / run
    assert parent.task_manager.list_tasks() == []
    assert json.loads(parent.run_list()) == []


# ─── 6. finished_run_injected_with_summary ───────────────────


def test_finished_run_injected_with_summary():
    """A2：完成摘要经 run_record（inject_summary=True）PUSH 回父上下文，并标 injected。"""
    parent = _agent()
    _seed_memories()
    _spy_build_with_stub(parent, text=_DELETE_PLAN)

    async def scenario():
        res = await parent._spawn_memory_consolidate()
        run_id = _run_id(res)
        await _wait_run_terminal(parent, run_id)
        return run_id

    run_id = asyncio.run(scenario())
    from nanocode.session import tree as _T
    from nanocode.session.manager import SessionManager as _SM
    if parent._session_mgr is None:
        parent._session_mgr = _SM.create("memsid")
    parent.agent_session.inject_finished_tasks()
    content = next(e.data["content"] for e in parent._session_mgr.entries()
                   if e.type == _T.CUSTOM_MESSAGE and e.data.get("customType") == "finished_tasks")
    assert "<system-reminder>" in content
    assert run_id in content
    assert "memory-curator" in content
    assert "archived" in content
    # 去重：run 标 injected，下一轮不再 PUSH
    assert run_record.read_status(run_id)["injected"] is True


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
        res = await parent._spawn_memory_consolidate()
        return await _wait_run_terminal(parent, _run_id(res))

    st = asyncio.run(scenario())
    assert st["status"] == "failed"
    assert "curator boom" in (st.get("error") or "")


# ─── 8. memory_tool_consolidate_action_delegates ─────────────


def test_memory_tool_consolidate_action_delegates():
    parent = _agent(memory_service=_markdown_memory_service())
    _seed_memories()
    _spy_build_with_stub(parent, text=_DELETE_PLAN)

    async def scenario():
        res = await parent._execute_tool_call("memory", {"action": "consolidate"})
        await _wait_run_terminal(parent, _run_id(res))
        return res

    res = asyncio.run(scenario())
    assert "run sess_" in res
    st = run_record.read_status(_run_id(res))
    assert st["agentType"] == MEMORY_CURATOR_TYPE
    assert st["status"] == "completed"
    # 单账本：tool 委派也不建 host task
    assert parent.task_manager.list_tasks() == []
