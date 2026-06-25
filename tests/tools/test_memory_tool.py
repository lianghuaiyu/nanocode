"""Memory tool boundary tests.

The `memory` tool module carries schema + a thin `run` (docs/24 Phase 3) that
forwards to `ctx.memory.execute`. Runtime execution is host-routed via
CapabilityRouter -> ctx.memory -> Agent.execute_memory_tool -> MemoryService.execute_tool.
"""
import asyncio
import inspect

from nanocode.tools import memory_tool as mt
from nanocode.memory.service import MemoryService, MemoryServiceConfig


class FakeHost:
    is_sub_agent = False

    async def spawn_memory_consolidate(self):
        return "consolidation started"


def _svc():
    return MemoryService(
        config=MemoryServiceConfig(backend="markdown"),
        cwd=".",
        agent_dir=".",
    )


def test_schema_registered():
    from nanocode.tools import REGISTRY
    names = set(REGISTRY.names())
    assert "memory" in names


def test_execute_dispatch_does_not_have_memory_handler():
    # docs/24 Phase 3：memory 现有自包含 run（经 ctx.memory 把手薄转发到 host.execute_memory_tool），
    # 但仍是 host-routed —— 由 CapabilityRouter 在 hook 段之前截走，**不**经 execute.py 通用 handler。
    from nanocode.tools import REGISTRY
    assert REGISTRY.get("memory").run is not None


def test_memory_schema_includes_host_actions():
    enum = mt.SCHEMA["input_schema"]["properties"]["action"]["enum"]
    assert "consolidate" in enum


def test_memory_tool_is_schema_plus_thin_run():
    # docs/24 Phase 3：memory_tool 现承载 schema + 薄 run（经 ctx.memory.execute 转发）；
    # 仍不 import store/backend、不实现任何 action —— run 只把 inp 转给 ctx.memory。
    assert hasattr(mt, "run")
    src = inspect.getsource(mt)
    assert "ctx.memory.execute" in src
    assert "import" not in src.split("def run", 1)[1]  # run 体内无任何 import（纯转发）


def test_service_add_search_list_read():
    svc = _svc()
    host = FakeHost()
    add = asyncio.run(svc.execute_tool(
        {"action": "add_note", "title": "pytest cmd", "kind": "project",
         "description": "how to test", "content": "pytest -q"},
        host=host,
    ))
    assert "Saved memory" in add
    listed = asyncio.run(svc.execute_tool({"action": "list"}, host=host))
    assert "pytest cmd" in listed
    search = asyncio.run(svc.execute_tool({"action": "search", "query": "pytest"}, host=host))
    assert "pytest" in search.lower()


def test_service_validation_errors():
    svc = _svc()
    host = FakeHost()
    assert "requires" in asyncio.run(svc.execute_tool({"action": "add_note", "title": "x"}, host=host))
    assert "Unknown memory action" in asyncio.run(svc.execute_tool({"action": "frobnicate"}, host=host))


def test_consolidate_delegates_to_host():
    out = asyncio.run(_svc().execute_tool({"action": "consolidate"}, host=FakeHost()))
    assert "consolidation started" in out
