import asyncio

from nanocode.tools import REGISTRY
from nanocode.tools.context import ToolContext, MemoryCap
from nanocode.capabilities.router import CapabilityRouter, classify_capability, Capability


def test_memory_tool_has_thin_run_handler():
    # docs/24 Phase 3：memory 现有自包含 run（薄转发到 ctx.memory），仍 host-routed。
    assert REGISTRY.get("memory").run is not None


def test_memory_classified_as_capability():
    assert classify_capability("memory") is Capability.MEMORY


class FakeHost:
    is_sub_agent = False
    session_id = "s"

    def __init__(self):
        self.received = None

    def tool_blocked_by_allowlist(self, name):
        return False

    def emit(self, event):
        return True

    def mint_tool_context(self, name):
        # Phase 3：host 铸造 per-call ctx；memory 工具经 ctx.memory.execute 转发回 host。
        return ToolContext(memory=MemoryCap(self))

    async def execute_memory_tool(self, inp):
        self.received = inp
        return f"routed:{inp.get('action')}"


def test_router_routes_memory_to_host():
    host = FakeHost()
    out = asyncio.run(CapabilityRouter().dispatch(host, "memory", {"action": "list"}))
    assert out == "routed:list" and host.received == {"action": "list"}


def test_router_rejects_unknown_keys():
    # additionalProperties=False on the closed schema -> validation rejects spoof keys
    out = asyncio.run(CapabilityRouter().dispatch(FakeHost(), "memory",
                                                  {"action": "list", "_cwd": "/etc"}))
    assert out.startswith("Error:")


def test_router_blocks_memory_when_not_in_allowlist():
    class Blocked(FakeHost):
        def tool_blocked_by_allowlist(self, name):
            return True
    out = asyncio.run(CapabilityRouter().dispatch(Blocked(), "memory", {"action": "list"}))
    assert "not permitted" in out
