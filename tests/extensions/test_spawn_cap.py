"""docs/26 阶段1 ②：受信 spawn 槽 ctx.spawn(SpawnCap)。

- 仅声明 `spawn:reserved` capability 的扩展拿到 ctx.spawn（memory_evolution → diagnostician）；
- SpawnCap.reserved 只接受被授予的 reserved 类型，且**签名无 tools/sandbox 入参**（不可提权）；
- 无 capability 的 host → ctx.spawn is None；
- SpawnCap.reserved 委托 thread.run_reserved_subagent（内核派生子 caps）。
"""
import asyncio
import inspect

import pytest

from nanocode.extensions import ExtensionHost
from nanocode.extensions.context import SpawnCap
from nanocode.extensions.errors import ExtensionRuntimeError
from nanocode.extensions.memory_evolution.manifest import MEMORY_DIAGNOSTICIAN_TYPE


class _FakeThread:
    def __init__(self):
        self._agent = None
        self.model = "claude-opus"
        self.calls = []

    def readonly_session(self):
        return None

    async def run_reserved_subagent(self, agent_type, prompt, *, model=None, timeout_ms=None):
        self.calls.append((agent_type, prompt, model, timeout_ms))
        return f"reserved:{agent_type}"


def _bound_system_host():
    host = ExtensionHost.load_system_extensions().activate_all()
    thread = _FakeThread()
    host.bind_runtime(thread, None)
    return host, thread


def test_spawn_cap_granted_to_capability_extension():
    host, _thread = _bound_system_host()
    ctx = host.create_context()
    assert isinstance(ctx.spawn, SpawnCap)
    # memory_evolution 声明 spawn:reserved 且贡献 diagnostician → 仅它在授予集。
    assert ctx.spawn._allowed == frozenset({MEMORY_DIAGNOSTICIAN_TYPE})
    # orchestration 扩展声明 spawn:orchestrate → 同一槽也解锁编排原语（docs/26 §0.6 阶段1）。
    assert ctx.spawn._can_orchestrate is True


def test_spawn_cap_reserved_delegates_to_kernel():
    host, thread = _bound_system_host()
    ctx = host.create_context()
    out = asyncio.run(ctx.spawn.reserved(MEMORY_DIAGNOSTICIAN_TYPE, "diagnose", model="m", timeout_ms=50))
    assert out == f"reserved:{MEMORY_DIAGNOSTICIAN_TYPE}"
    assert thread.calls == [(MEMORY_DIAGNOSTICIAN_TYPE, "diagnose", "m", 50)]


def test_spawn_cap_rejects_non_granted_type():
    host, _thread = _bound_system_host()
    ctx = host.create_context()
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(ctx.spawn.reserved("coder", "escalate pls"))


def test_spawn_cap_signature_has_no_tools_or_sandbox():
    params = inspect.signature(SpawnCap.reserved).parameters
    assert "agent_type" in params and "prompt" in params
    assert "tools" not in params and "sandbox" not in params and "sandbox_profile" not in params


def test_no_spawn_cap_without_capability():
    # 无任何声明 spawn:reserved 的扩展 → ctx.spawn is None。
    host = ExtensionHost([]).activate_all()
    host.bind_runtime(_FakeThread(), None)
    ctx = host.create_context()
    assert ctx.spawn is None


def test_stale_spawn_cap_fails_loud():
    host, _thread = _bound_system_host()
    ctx = host.create_context()
    host.invalidate("dispose")
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(ctx.spawn.reserved(MEMORY_DIAGNOSTICIAN_TYPE, "p"))
