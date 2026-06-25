"""docs/22 Phase 5: end-to-end /memory optimize through RuntimeThread.run_extension_task.

A real RuntimeThread + a real ExtensionHost (bound to fake services) dispatch the
memory_optimize task to the extension handler, which runs the deterministic
optimizer and lands a terminal task status. The optimization worker is host-only:
no memory tool, no sub-agent, no MCP/network/shell (docs/22 §9.1.9).
"""
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from nanocode.agent import AgentRuntime, RuntimeThread
from nanocode.extensions import ExtensionHost
from nanocode.memory.retrieval_eval import RetrievalEvalCase
from nanocode.memory import retrieval_config_store as RCS
from nanocode.tasks.manager import TaskManager


@dataclass
class _Hit:
    entry_id: str
    lossless_restatement: str
    keywords: tuple = ()


class _FakeEngine:
    def __init__(self, hits_for, root):
        self._hits_for, self._root = hits_for, root

    def stats(self):
        return {"root": self._root}

    def retrieve_with_config(self, query, config, *, limit):
        return self._hits_for(query, config)[:limit]


class _FakeAgent:
    def __init__(self, mem):
        self.session_id = "sess-opt"
        self.task_manager = TaskManager()
        self._background_tasks = set()
        self._event_subscribers = []
        self.model = "claude-opus"
        self._memory_service = mem
        self._session_mgr = None
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def emit(self, ev):
        pass


def test_run_extension_task_promotes(monkeypatch, tmp_path):
    cases = [RetrievalEvalCase(f"c{i}", f"q{i}", f"answer {i} body", (f"answer {i} body",))
             for i in range(5)]
    ans = {c.question: c.answer for c in cases}

    def hits_for(q, cfg):
        return [_Hit("e", ans[q])] if cfg.semantic_top_k >= 30 else [_Hit("e", "irrelevant")]

    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED", "5")
    monkeypatch.setenv("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS", "3")
    monkeypatch.setattr("nanocode.memory.retrieval_eval.cases_from_confirmed", lambda: cases)

    eng = _FakeEngine(hits_for, str(tmp_path))
    mem = SimpleNamespace(backend_name="simplemem", backend=SimpleNamespace(engine=eng))
    agent = _FakeAgent(mem)

    async def _scenario():
        rt = AgentRuntime()
        thread = rt.register(RuntimeThread(rt, agent, SimpleNamespace(agent=agent)))
        host = ExtensionHost.load_system_extensions().activate_all()
        services = SimpleNamespace(cwd=str(tmp_path), memory_service=mem, extension_host=host)
        thread._extension_host = host
        host.bind_runtime(thread, services)
        msg = await thread.run_extension_task("memory_optimize", {})
        assert "task " in msg
        await asyncio.gather(*list(agent._background_tasks))
        return agent.task_manager

    tm = asyncio.run(_scenario())
    tasks = tm.list_tasks()
    assert tasks and tasks[0].kind == "memory_optimize"
    assert tasks[0].status == "completed"
    assert "promoted" in (tasks[0].result_summary or "")
    assert RCS.load_retrieval_config(str(tmp_path)).semantic_top_k == 30
