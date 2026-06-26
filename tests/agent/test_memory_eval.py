import asyncio
import json
import re

from nanocode.agent.engine import Agent
from nanocode.paths import project_memory_dir
from nanocode.runs.models import TERMINAL_RUN_STATUSES
from nanocode.subagents import run_record
from nanocode.subagents.prompts import MEMORY_EVAL_CURATOR_TYPE
from nanocode.memory import eval_store
from nanocode.memory.service import MemoryService, MemoryServiceConfig
from nanocode.session import v2 as _v2


SID = "evalsid"


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    kw.setdefault(
        "memory_service",
        MemoryService(config=MemoryServiceConfig(backend="markdown"),
                      cwd=".", agent_dir="."),
    )
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


def _run_id(res: str) -> str:
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
        return await _wait_run_terminal(parent, _run_id(res)), res

    st, res = asyncio.run(scenario())
    assert captured["kw"]["agent_type"] == MEMORY_EVAL_CURATOR_TYPE
    assert captured["kw"]["background"] is True
    assert st["agentType"] == MEMORY_EVAL_CURATOR_TYPE
    assert st["status"] == "completed"
    assert st["injectSummary"] is True
    assert "2 pending eval candidate" in (st.get("resultSummary") or "")
    pend = eval_store.list_pending()
    assert len(pend) == 2
    # 宿主强制填 session_id = self.session_id
    assert all(c.source.get("session_id") == SID for c in pend)
    # 单账本：不再镜像 host TaskManager
    assert parent.task_manager.list_tasks() == []


def test_invalid_candidate_skipped_not_failed():
    parent = _agent(); _seed_session(); _seed_memories()
    _spy_build_with_stub(parent, text=_ONE_BAD)

    async def scenario():
        res = await parent._spawn_memory_eval()
        return await _wait_run_terminal(parent, _run_id(res))

    st = asyncio.run(scenario())
    assert st["status"] == "completed"          # 非法候选不让 run failed
    summary = st.get("resultSummary") or ""
    assert "1 pending eval candidate" in summary
    assert "1 skipped" in summary
    assert len(eval_store.list_pending()) == 1


def test_bad_json_completed_zero():
    parent = _agent(); _seed_session(); _seed_memories()
    _spy_build_with_stub(parent, text="not json {")

    async def scenario():
        res = await parent._spawn_memory_eval()
        return await _wait_run_terminal(parent, _run_id(res))

    st = asyncio.run(scenario())
    assert st["status"] == "completed"
    assert "0 pending eval candidate" in (st.get("resultSummary") or "")
    assert eval_store.list_pending() == []


def test_no_memories_skips_no_task():
    parent = _agent(); _seed_session()
    project_memory_dir()  # 空
    res = asyncio.run(parent._spawn_memory_eval())
    assert "No memories" in res
    assert parent.task_manager.list_tasks() == []
    assert json.loads(parent.run_list()) == []


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
        res = await parent._spawn_memory_eval()
        return await _wait_run_terminal(parent, _run_id(res))

    st = asyncio.run(scenario())
    assert st["status"] == "failed"
    assert "eval curator boom" in (st.get("error") or "")
