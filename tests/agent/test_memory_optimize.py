"""Task 7: 记忆优化线 _spawn_memory_optimize + _run_memory_optimize（C 子阶段）。

纯宿主计算（无 curator/subagent）：backend duck-type 判定 → prune confirmed evals →
阈值门控 → simplemem.optimize（**测试 monkeypatch，绝不真跑 EvolveMem**）→ 原子落
evolve_config.json。失败 → task failed 且旧 config 原样保留。

阈值默认 5（人工最终决策覆盖 #1）；测试用 env 显式设阈值以确定行为。
"""

import asyncio
import json
from dataclasses import dataclass

import pytest

from nanocode.agent.engine import Agent
from nanocode.paths import project_memory_dir
from nanocode.memory import eval_store, maintenance
from nanocode.session import v2 as _v2


SID = "optsid"


def _agent(backend=None, **kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id=SID,
                 memory_backend=backend, **kw)


def _seed_session():
    _v2.write_state(SID, {"session_id": SID, "tasks": {}, "subagents": {}})


def _seed_confirmed(n: int):
    """造 n 条 confirmed（add_pending 真跑 + confirm）。memory_ref 指向存活 .md。"""
    mem = project_memory_dir()
    (mem / "m.md").write_text("---\nname: m\n---\nfact body about v2 Q1")
    for i in range(n):
        c = eval_store.MemoryEvalCandidate(
            question=f"q{i}", answer=f"a{i}",
            source={"session_id": SID, "memory_ref": "m.md"},
            evidence=[f"e{i}"],
        )
        cid = eval_store.add_pending(c)
        assert eval_store.confirm(cid)


class _FakeBackend:
    """duck-type simplemem backend：name + _system（_system 任意对象，被 stub 吃掉）。"""
    name = "simplemem"

    def __init__(self):
        self._system = object()


class _Markdownish:
    name = "markdown"


@dataclass
class _FakeConfig:
    """假 Config（dataclass，asdict 有效，对齐真实 simplemem.Config）。"""
    k_kw: int = 7
    evolution_rounds: int = 3
    evolved: bool = True


def _stub_optimize(monkeypatch, *, raises=False, captured=None):
    import nanocode._vendor.simplemem as sm

    def _opt(mem, dev_questions, max_rounds=7, **kw):
        if captured is not None:
            captured["mem"] = mem
            captured["dev"] = dev_questions
            captured["max_rounds"] = max_rounds
        if raises:
            raise RuntimeError("optimize boom")
        return _FakeConfig()

    monkeypatch.setattr(sm, "optimize", _opt)


async def _wait_terminal(parent, task_id, tries=200, delay=0.02):
    from nanocode.tasks.models import TERMINAL_TASK_STATUSES
    for _ in range(tries):
        t = parent.task_manager.get_task(task_id)
        if t and t.status in TERMINAL_TASK_STATUSES:
            return t
        await asyncio.sleep(delay)
    return parent.task_manager.get_task(task_id)


def test_runs_optimize_and_saves_config(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "2")
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS", "5")
    _seed_session(); _seed_confirmed(3)
    captured = {}
    _stub_optimize(monkeypatch, captured=captured)
    parent = _agent(backend=_FakeBackend())

    async def scenario():
        res = await parent._spawn_memory_optimize()
        return await _wait_terminal(parent, "task-001"), res

    rec, res = asyncio.run(scenario())
    assert rec.kind == "memory_optimize"
    assert rec.owner_agent_id is None         # task only，无 subagent
    assert rec.status == "completed"
    assert "evolved config saved" in (rec.result_summary or "")
    # optimize 收到 3 个 dev questions + env max_rounds=5
    assert len(captured["dev"]) == 3
    assert captured["max_rounds"] == 5
    # finalized mem 实例就是 backend._system，被原样透传给 optimize
    assert captured["mem"] is parent._memory_backend._system
    # 真落 evolve_config.json
    cfg = maintenance.load_evolve_config()
    assert cfg is not None and cfg.get("k_kw") == 7 and cfg.get("evolved") is True
    # 不注册 child run
    assert json.loads(parent.run_list()) == []


def test_below_threshold_skipped(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "10")
    _seed_session(); _seed_confirmed(2)
    called = {"n": 0}
    import nanocode._vendor.simplemem as sm
    monkeypatch.setattr(sm, "optimize",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _FakeConfig())
    parent = _agent(backend=_FakeBackend())

    async def scenario():
        await parent._spawn_memory_optimize()
        return await _wait_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "completed"
    assert "skipped" in (rec.result_summary or "").lower()
    assert "2" in (rec.result_summary or "") and "10" in (rec.result_summary or "")
    assert called["n"] == 0                # optimize 未被调用
    assert maintenance.load_evolve_config() is None   # 未落 config


def test_prune_called_before_threshold(monkeypatch):
    """confirmed 引用的 .md 被删 → prune 应清掉它 → 低于阈值 → skipped。"""
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "2")
    _seed_session(); _seed_confirmed(2)
    # 删掉 source .md，使两条 confirmed 成为孤儿
    (project_memory_dir() / "m.md").unlink()
    import nanocode._vendor.simplemem as sm
    monkeypatch.setattr(sm, "optimize", lambda *a, **k: _FakeConfig())
    parent = _agent(backend=_FakeBackend())

    async def scenario():
        await parent._spawn_memory_optimize()
        return await _wait_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    # prune 清掉 2 条 → confirmed 变 0 < 2 → skipped
    assert rec.status == "completed"
    assert "skipped" in (rec.result_summary or "").lower()
    assert eval_store.list_confirmed() == []


def test_backend_not_simplemem_unavailable(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "1")
    _seed_session(); _seed_confirmed(2)
    parent = _agent(backend=_Markdownish())

    async def scenario():
        await parent._spawn_memory_optimize()
        return await _wait_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "completed"
    assert "unavailable" in (rec.result_summary or "").lower()
    assert "not simplemem" in (rec.result_summary or "").lower()


def test_optimize_failure_keeps_old_config(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "2")
    _seed_session(); _seed_confirmed(3)
    # 先放一份旧 config
    maintenance.save_evolve_config({"k_kw": 1, "evolved": False})
    _stub_optimize(monkeypatch, raises=True)
    parent = _agent(backend=_FakeBackend())

    async def scenario():
        await parent._spawn_memory_optimize()
        return await _wait_terminal(parent, "task-001")

    rec = asyncio.run(scenario())
    assert rec.status == "failed"
    assert "optimize boom" in (rec.error or "")
    # 旧 config 原样保留（optimize 抛异常 → 不调 save）
    cfg = maintenance.load_evolve_config()
    assert cfg is not None and cfg.get("k_kw") == 1 and cfg.get("evolved") is False
