import asyncio
import json

import pytest

from nanocode.agent.engine import Agent
from nanocode.paths import project_memory_dir
from nanocode.subagents.prompts import MEMORY_EVAL_CURATOR_TYPE
from nanocode.memory import eval_store
from nanocode.session import v2 as _v2


SID = "evalsid"


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id=SID, **kw)


def _seed_session():
    # 让 source.session_id 校验通过：把 SID 注册成 v2 session（is_v2_session→True）。
    _v2.write_state(SID, {"session_id": SID, "tasks": {}, "subagents": {}})


def _seed_memories():
    mem = project_memory_dir()
    (mem / "project_goals.md").write_text(
        "---\nname: goals\n---\nWe want to ship v2 by end of Q1."
    )
    return mem


def _spy_build_with_stub(parent, *, text, captured=None, tokens=None):
    tokens = tokens or {"input": 9, "output": 5}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)
        if captured is not None:
            captured["kw"] = kw
        async def _ro(prompt: str) -> dict:
            return {"text": text, "tokens": tokens}
        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


async def _wait_terminal(parent, task_id, tries=200, delay=0.02):
    from nanocode.tasks.models import TERMINAL_TASK_STATUSES
    for _ in range(tries):
        t = parent.task_manager.get_task(task_id)
        if t and t.status in TERMINAL_TASK_STATUSES:
            return t
        await asyncio.sleep(delay)
    return parent.task_manager.get_task(task_id)


_GOOD = json.dumps({"candidates": [
    {"question": "When ship v2?", "answer": "End of Q1.",
     "category": "general", "confidence": 0.9,
     "evidence": ["We want to ship v2 by end of Q1."],
     "source": {"memory_ref": "project_goals.md"}},
    {"question": "What is the v2 target?", "answer": "Q1.",
     "category": "general", "confidence": 0.8,
     "evidence": ["ship v2 by end of Q1"],
     "source": {"memory_ref": "project_goals.md"}},
]})

# 一条非法（缺 evidence）→ 应被跳过
_ONE_BAD = json.dumps({"candidates": [
    {"question": "ok q", "answer": "ok a", "evidence": ["e"],
     "source": {"memory_ref": "project_goals.md"}},
    {"question": "bad q", "answer": "bad a", "evidence": [],   # 校验失败
     "source": {"memory_ref": "project_goals.md"}},
]})


def test_generates_pending_candidates():
    parent = _agent(); _seed_session(); _seed_memories()
    captured = {}
    _spy_build_with_stub(parent, text=_GOOD, captured=captured)

    async def scenario():
        res = await parent._spawn_memory_eval()
        return await _wait_terminal(parent, "task-001"), res

    rec, res = asyncio.run(scenario())
    assert captured["kw"]["agent_type"] == MEMORY_EVAL_CURATOR_TYPE
    assert captured["kw"]["background"] is True
    assert rec.kind == "memory_eval"
    assert rec.status == "completed"
    assert "2 pending eval candidate" in (rec.result_summary or "")
    pend = eval_store.list_pending()
    assert len(pend) == 2
    # 宿主强制填 session_id = self.session_id
    assert all(c.source.get("session_id") == SID for c in pend)


def test_invalid_candidate_skipped_not_failed():
    parent = _agent(); _seed_session(); _seed_memories()
    _spy_build_with_stub(parent, text=_ONE_BAD)

    async def scenario():
        await parent._spawn_memory_eval()
        return await _wait_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "completed"          # 非法候选不让 task failed
    assert "1 pending eval candidate" in (rec.result_summary or "")
    assert "1 skipped" in (rec.result_summary or "")
    assert len(eval_store.list_pending()) == 1


def test_bad_json_completed_zero():
    parent = _agent(); _seed_session(); _seed_memories()
    _spy_build_with_stub(parent, text="not json {")

    async def scenario():
        await parent._spawn_memory_eval()
        return await _wait_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "completed"
    assert "0 pending eval candidate" in (rec.result_summary or "")
    assert eval_store.list_pending() == []


def test_no_memories_skips_no_task():
    parent = _agent(); _seed_session()
    project_memory_dir()  # 空
    res = asyncio.run(parent._spawn_memory_eval())
    assert "No memories" in res
    assert parent.task_manager.list_tasks() == []
    assert parent.task_manager.list_subagents() == []


def test_curator_error_marks_failed():
    parent = _agent(); _seed_session(); _seed_memories()
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)
        async def _ro(prompt):
            raise RuntimeError("eval curator boom")
        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy

    async def scenario():
        await parent._spawn_memory_eval()
        return await _wait_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "failed"
    assert "eval curator boom" in (rec.error or "")
