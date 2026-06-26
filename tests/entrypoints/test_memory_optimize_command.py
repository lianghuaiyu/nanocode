"""docs/22 Phase 5: memory_optimize task handler control flow + promotion.

Drives extensions/memory_evolution/tasks.run_memory_optimize_task with fakes for
each gate: no service / unsupported backend / insufficient confirmed / promotion.
"""
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from nanocode.extensions.memory_evolution.tasks import run_memory_optimize_task
from nanocode.memory.engines.simplemem.retrieval_config import RetrievalConfig
from nanocode.memory.retrieval_eval import RetrievalEvalCase
from nanocode.memory import retrieval_config_store as RCS


@dataclass
class _Hit:
    entry_id: str
    lossless_restatement: str
    keywords: tuple = ()


class _FakeEngine:
    def __init__(self, hits_for, root):
        self._hits_for = hits_for
        self._root = root

    def stats(self):
        return {"root": self._root}

    def retrieve_with_config(self, query, config, *, limit):
        return self._hits_for(query, config)[:limit]


class _FakeTasks:
    def __init__(self):
        self.updates = []

    def update_task(self, task_id, **fields):
        self.updates.append((task_id, fields))

    def last(self):
        return self.updates[-1][1] if self.updates else {}


def _memory(backend_name, engine=None):
    backend = SimpleNamespace(engine=engine)
    return SimpleNamespace(backend_name=backend_name, backend=backend)


def _ctx(memory):
    return SimpleNamespace(memory=memory, tasks=_FakeTasks(), thread=None)


def _run(ctx, payload=None):
    asyncio.run(run_memory_optimize_task(ctx, payload or {}, task_id="t1"))
    return ctx.tasks.last()


def test_no_memory_service():
    ctx = _ctx(None)
    out = _run(ctx)
    assert out["status"] == "completed"
    assert "no MemoryService" in out["result_summary"]


def test_unsupported_backend():
    ctx = _ctx(_memory("markdown"))
    out = _run(ctx)
    assert out["status"] == "completed"
    assert "unsupported backend" in out["result_summary"]


def test_insufficient_confirmed(monkeypatch, tmp_path):
    eng = _FakeEngine(lambda q, c: [], str(tmp_path))
    ctx = _ctx(_memory("simplemem", eng))
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "5")
    monkeypatch.setattr("nanocode.memory.retrieval_eval.cases_from_confirmed",
                        lambda: [RetrievalEvalCase("c0", "q", "a", ("a",))])
    out = _run(ctx)
    assert out["status"] == "completed"
    assert "not enough confirmed" in out["result_summary"]
    assert "1/5" in out["result_summary"]


def test_promotion_writes_config(monkeypatch, tmp_path):
    cases = [RetrievalEvalCase(f"c{i}", f"q{i}", f"answer {i} body", (f"answer {i} body",))
             for i in range(5)]
    ans = {c.question: c.answer for c in cases}

    def hits_for(q, cfg):
        if cfg.semantic_top_k >= 30:
            return [_Hit("e", ans[q])]
        return [_Hit("e", "irrelevant")]

    eng = _FakeEngine(hits_for, str(tmp_path))
    ctx = _ctx(_memory("simplemem", eng))
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "5")
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS", "3")
    monkeypatch.setattr("nanocode.memory.retrieval_eval.cases_from_confirmed", lambda: cases)
    out = _run(ctx)
    assert out["status"] == "completed"
    assert "promoted" in out["result_summary"]
    # live config persisted at the engine store root
    assert RCS.load_retrieval_config(str(tmp_path)).semantic_top_k == 30


def test_no_promotion_leaves_config_unchanged(monkeypatch, tmp_path):
    cases = [RetrievalEvalCase(f"c{i}", f"q{i}", f"answer {i}", (f"answer {i}",))
             for i in range(5)]
    eng = _FakeEngine(lambda q, c: [_Hit("e", "always irrelevant")], str(tmp_path))
    ctx = _ctx(_memory("simplemem", eng))
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "5")
    monkeypatch.setattr("nanocode.memory.retrieval_eval.cases_from_confirmed", lambda: cases)
    out = _run(ctx)
    assert out["status"] == "completed"
    assert "no promotion" in out["result_summary"]
    assert not (tmp_path / "retrieval_config.json").exists()
