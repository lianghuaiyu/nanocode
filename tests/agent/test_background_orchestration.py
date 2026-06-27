"""docs/26 D6：后台编排（detached chain / parallel + group_id + 整组 cancel）。

A+ 内核原语化：steps/tasks + run_in_background → 立即返回 group id；
- parallel：N 个独立 detached run（复用 spawn_background_subagent），各打 groupId / inject_summary；
- chain：一个 detached coordinator 顺序跑 N 步（复用内核 spawn_subagent），{previous} 串接、fail-stop；
- 完成经 FinishedTasksProvider PUSH 回父（inject_summary=True）；
- run_cancel <group_id> 级联取消整组（running + queued + chain coordinator 在飞步）；单子不动兄弟。
"""

import asyncio
import json
import re

from nanocode.agent.engine import Agent
from nanocode.runs.models import TERMINAL_RUN_STATUSES
from nanocode.subagents import run_record


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="orchsid", **kw)


def _spy_build(parent, *, run_once=None, text="done", record=None):
    """spy _build_sub_agent：注入 stub run_once（写 child 树历史，使 finish_run_record 有 entry）。"""
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)
        if run_once is not None:
            sub.run_once = run_once(sub, kw)
        else:
            async def _ro(prompt: str) -> dict:
                if record is not None:
                    record.append({"agent_type": kw.get("agent_type"),
                                   "artifact_id": kw.get("artifact_id"), "prompt": prompt})
                if sub._session_mgr is not None:
                    sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
                    sub.agent_session.record_provider_messages({"role": "assistant", "content": text})
                return {"text": text, "tokens": {"input": 1, "output": 1}}
            sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


def _gid(text: str) -> str:
    m = re.search(r"group (orch_[A-Za-z0-9]+)", text)
    assert m, text
    return m.group(1)


def _run_ids(text: str) -> list[str]:
    return re.findall(r"sess_[A-Za-z0-9]+", text)


async def _wait_run_terminal(run_id, tries=300, delay=0.02):
    for _ in range(tries):
        try:
            status = run_record.read_status(run_id)
        except FileNotFoundError:
            status = None
        if status and status["status"] in TERMINAL_RUN_STATUSES:
            return status
        await asyncio.sleep(delay)
    return run_record.read_status(run_id)


async def _wait_group_terminal(parent, gid, *, expected, tries=400, delay=0.02):
    for _ in range(tries):
        recs = [r for r in json.loads(parent.run_list()) if r.get("group_id") == gid]
        terminal = [r for r in recs if r["status"] in TERMINAL_RUN_STATUSES]
        if len(terminal) >= expected:
            return recs
        await asyncio.sleep(delay)
    return [r for r in json.loads(parent.run_list()) if r.get("group_id") == gid]


# ─── parallel 后台编排 ────────────────────────────────────────


def test_parallel_background_returns_group_and_tags_records():
    parent = _agent()
    _spy_build(parent, text="body")

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "fan", "run_in_background": True,
            "tasks": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]})
        gid, run_ids = _gid(res), _run_ids(res)
        for rid in run_ids:
            await _wait_run_terminal(rid)
        return res, gid, run_ids

    res, gid, run_ids = asyncio.run(scenario())
    assert gid.startswith("orch_") and len(run_ids) == 3
    assert "do not poll" in res
    recs = {r["child_session_id"]: r for r in json.loads(parent.run_list())}
    for rid in run_ids:
        assert recs[rid]["group_id"] == gid                 # group_id 端到端外流
        assert recs[rid]["inject_summary"] is True
        assert recs[rid]["status"] == "completed"
        assert recs[rid]["background"] is True


def test_parallel_background_injects_each_summary_then_dedups():
    parent = _agent()
    _spy_build(parent, text="body")

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "fan", "run_in_background": True,
            "tasks": [{"prompt": "a"}, {"prompt": "b"}]})
        for rid in _run_ids(res):
            await _wait_run_terminal(rid)
        return res

    res = asyncio.run(scenario())
    from nanocode.context.providers import FinishedTasksProvider
    prov = FinishedTasksProvider(parent)
    pack = prov.collect()
    assert pack is not None
    for rid in _run_ids(res):
        assert rid in pack.content
    assert "parallel task" in pack.content
    prov.commit()                                            # 标 injected
    assert FinishedTasksProvider(parent).collect() is None   # 去重：下一轮不再 PUSH


def test_parallel_background_all_or_nothing_prevalidation():
    parent = _agent()
    _spy_build(parent)

    async def scenario():
        # 第二个 task 的 context.mode 非法 → 整体拒绝，绝不 spawn 一半留孤儿子。
        return await parent._execute_agent_tool({
            "description": "fan", "run_in_background": True,
            "tasks": [{"prompt": "a"}, {"prompt": "b", "context": {"mode": "bogus"}}]})

    res = asyncio.run(scenario())
    assert res.startswith("Error") and "tasks[2]" in res
    assert json.loads(parent.run_list()) == []               # 无孤儿 detached run


# ─── chain 后台编排 ───────────────────────────────────────────


def test_chain_background_threads_previous_sequentially():
    parent = _agent()
    record = []

    def _run_once(sub, kw):
        async def _ro(prompt: str) -> dict:
            record.append({"agent_type": kw.get("agent_type"), "prompt": prompt})
            out = f"OUT{len(record)}"
            if sub._session_mgr is not None:
                sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
                sub.agent_session.record_provider_messages({"role": "assistant", "content": out})
            return {"text": out, "tokens": {"input": 1, "output": 1}}
        return _ro

    _spy_build(parent, run_once=_run_once)

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "chain", "run_in_background": True,
            "steps": [{"type": "explore", "prompt": "one"},
                      {"type": "coder", "prompt": "two: {previous}"}]})
        gid = _gid(res)
        await _wait_group_terminal(parent, gid, expected=2)
        return res, gid

    res, gid = asyncio.run(scenario())
    assert "background chain group" in res
    assert [r["agent_type"] for r in record] == ["explore", "coder"]   # 顺序执行
    assert "OUT1" in record[1]["prompt"] and "{previous}" not in record[1]["prompt"]  # {previous} 串接
    recs = [r for r in json.loads(parent.run_list()) if r["group_id"] == gid]
    assert len(recs) == 2
    assert all(r["inject_summary"] and r["status"] == "completed" for r in recs)


def test_chain_background_fail_stops_remaining_steps():
    parent = _agent()
    calls = []

    def _run_once(sub, kw):
        async def _ro(prompt: str) -> dict:
            calls.append(kw.get("agent_type"))
            if len(calls) == 1:
                raise RuntimeError("step boom")
            return {"text": "x", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build(parent, run_once=_run_once)

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "chain", "run_in_background": True,
            "steps": [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}]})
        gid = _gid(res)
        await _wait_group_terminal(parent, gid, expected=1)
        await asyncio.sleep(0.1)            # 给 coordinator 机会去（不该）起第二步
        return gid

    gid = asyncio.run(scenario())
    assert calls == ["coder"]               # 仅首步跑过；fail-stop 终止链
    recs = [r for r in json.loads(parent.run_list()) if r["group_id"] == gid]
    assert len(recs) == 1 and recs[0]["status"] == "failed"


# ─── cancel 级联 ──────────────────────────────────────────────


def _slow_run_once(started):
    def _factory(sub, kw):
        async def _ro(prompt: str) -> dict:
            started.append(kw.get("artifact_id"))
            await asyncio.sleep(60)
            return {"text": "never", "tokens": {"input": 0, "output": 0}}
        return _ro
    return _factory


def test_cancel_group_cancels_all_running_members():
    parent = _agent()
    started: list = []
    _spy_build(parent, run_once=_slow_run_once(started))

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "fan", "run_in_background": True,
            "tasks": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]})
        gid, run_ids = _gid(res), _run_ids(res)
        for _ in range(300):
            if len(started) >= 3:
                break
            await asyncio.sleep(0.01)
        msg = await parent.run_cancel(gid)
        statuses = {rid: (await _wait_run_terminal(rid))["status"] for rid in run_ids}
        return gid, msg, statuses

    gid, msg, statuses = asyncio.run(scenario())
    assert "group" in msg and gid in msg
    assert statuses and all(s == "cancelled" for s in statuses.values())


def test_cancel_group_cancels_queued_members(monkeypatch):
    parent = _agent()
    monkeypatch.setattr(parent._subagents, "max_threads", lambda: 1)   # 1 跑 + 2 排队
    started: list = []
    _spy_build(parent, run_once=_slow_run_once(started))

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "fan", "run_in_background": True,
            "tasks": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]})
        gid, run_ids = _gid(res), _run_ids(res)
        for _ in range(200):                  # 等队头进入 run_once（其余 queued）
            if started:
                break
            await asyncio.sleep(0.01)
        # 应有 queued 成员
        statuses_before = {rid: run_record.read_status(rid)["status"] for rid in run_ids}
        msg = await parent.run_cancel(gid)
        statuses = {rid: (await _wait_run_terminal(rid))["status"] for rid in run_ids}
        return gid, msg, statuses_before, statuses

    gid, msg, before, statuses = asyncio.run(scenario())
    assert "queued" in before.values()                       # 确有排队（否则非此场景）
    assert all(s == "cancelled" for s in statuses.values())  # running + queued 全 cancelled


def test_cancel_chain_coordinator_stops_inflight_and_no_further_steps():
    parent = _agent()
    started = asyncio.Event()
    calls: list = []

    def _run_once(sub, kw):
        async def _ro(prompt: str) -> dict:
            calls.append(kw.get("agent_type"))
            started.set()
            await asyncio.sleep(60)
            return {"text": "never", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build(parent, run_once=_run_once)

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "chain", "run_in_background": True,
            "steps": [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}]})
        gid = _gid(res)
        await asyncio.wait_for(started.wait(), timeout=2.0)
        msg = await parent.run_cancel(gid)
        recs = await _wait_group_terminal(parent, gid, expected=1)
        await asyncio.sleep(0.1)             # 确认不再起后续步
        return gid, msg, recs

    gid, msg, recs = asyncio.run(scenario())
    assert gid in msg
    assert len(calls) == 1                  # 仅在飞步；后续步未起跑
    recs = [r for r in json.loads(parent.run_list()) if r["group_id"] == gid]
    assert len(recs) == 1 and recs[0]["status"] == "cancelled"


def test_cancel_single_member_leaves_siblings_running():
    parent = _agent()
    started: list = []
    _spy_build(parent, run_once=_slow_run_once(started))

    async def scenario():
        res = await parent._execute_agent_tool({
            "description": "fan", "run_in_background": True,
            "tasks": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]})
        gid, run_ids = _gid(res), _run_ids(res)
        for _ in range(300):
            if len(started) >= 3:
                break
            await asyncio.sleep(0.01)
        msg = await parent.run_cancel(run_ids[0])            # 仅取消第一个子
        st0 = (await _wait_run_terminal(run_ids[0]))["status"]
        await asyncio.sleep(0.1)
        siblings = {rid: run_record.read_status(rid)["status"] for rid in run_ids[1:]}
        await parent.run_cancel(gid)                          # 清理其余
        return msg, st0, siblings

    msg, st0, siblings = asyncio.run(scenario())
    assert "run" in msg and "group" not in msg               # 单子取消，非整组
    assert st0 == "cancelled"
    assert all(s == "running" for s in siblings.values())    # 兄弟不受影响
