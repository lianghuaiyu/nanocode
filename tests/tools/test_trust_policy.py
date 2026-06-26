"""docs/24 Phase 5：信任档能力策略 + register namespace 规则(对 builtin 零行为变更)。

锚点：
- policy_for_trust 三档(BUILTIN 全集 / TRUSTED 读类 / UNTRUSTED ∅)。
- _granted_capabilities = needs ∩ policy；BUILTIN 工具 granted == needs(零行为变更等价锚)。
- register fail-loud：外部撞内置名 / 外部缺前缀 / 非 TRUSTED 重名;TRUSTED 可覆盖;builtin 正常。
"""

import pytest

from nanocode.agent.engine import Agent
from nanocode.tools import REGISTRY, Tool
from nanocode.tools.registry import ToolRegistry
from nanocode.tools.types import (
    Capability, ToolSource, Trust, policy_for_trust,
)


# ─── policy_for_trust 表 ────────────────────────────────────────────────────────

def test_policy_untrusted_is_empty():
    assert policy_for_trust(Trust.UNTRUSTED) == frozenset()


def test_policy_trusted_is_read_class():
    assert policy_for_trust(Trust.TRUSTED) == frozenset({
        Capability.FS_READ, Capability.TASKS, Capability.SESSION_READ,
    })


def test_policy_builtin_is_full_set():
    assert policy_for_trust(Trust.BUILTIN) == frozenset(Capability)
    # 全集应含每一个声明的能力(防新增 Capability 时表漏更新)。
    assert len(policy_for_trust(Trust.BUILTIN)) == len(list(Capability))


# ─── _granted_capabilities：BUILTIN 等价锚 + UNTRUSTED 清零 ─────────────────────

def test_granted_for_builtin_tools_equals_needs():
    # 零行为变更等价锚：所有内置工具 granted == needs(needs ∩ 全集 = needs)。
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    for n in REGISTRY.names():
        tool = REGISTRY.get(n)
        assert a._granted_capabilities(tool) == frozenset(tool.needs), n


def test_granted_for_untrusted_tool_is_empty():
    # 假 UNTRUSTED 工具即便声明 needs,granted 被策略夹成 ∅。
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    fake = Tool(
        schema={"name": "ext__demo__x", "input_schema": {"type": "object", "properties": {}}},
        needs=frozenset({Capability.MEMORY, Capability.SPAWN, Capability.FS_READ}),
        source=ToolSource.EXT,
        trust=Trust.UNTRUSTED,
    )
    assert a._granted_capabilities(fake) == frozenset()


def test_granted_for_trusted_tool_intersects_read_class():
    # TRUSTED 工具声明读+写,只授予读类(写被策略夹掉)。
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    fake = Tool(
        schema={"name": "ext__demo__y", "input_schema": {"type": "object", "properties": {}}},
        needs=frozenset({Capability.FS_READ, Capability.FS_WRITE, Capability.TASKS}),
        source=ToolSource.EXT,
        trust=Trust.TRUSTED,
    )
    assert a._granted_capabilities(fake) == frozenset({Capability.FS_READ, Capability.TASKS})


# ─── register 规则 ──────────────────────────────────────────────────────────────

def _ext_tool(name, *, trust=Trust.UNTRUSTED, source=ToolSource.EXT):
    return Tool(
        schema={"name": name, "input_schema": {"type": "object", "properties": {}}},
        source=source,
        trust=trust,
    )


def test_builtin_registration_passes_normally():
    # builtin(_ALL 一次性注册)正常通过 —— from_builtins 不抛。
    reg = ToolRegistry.from_builtins([REGISTRY.get(n) for n in REGISTRY.names()])
    assert set(reg.names()) == set(REGISTRY.names())


def test_external_shadowing_builtin_name_fails_loud():
    # reserved-builtin：既有 BUILTIN tool 被外部 source 撞名 → 拒。用带前缀的占位内置名
    # 隔离验证(避开 forced-namespace 先行拦截),证明 reserved 规则独立成立。
    reg = ToolRegistry()
    builtin_placeholder = Tool(
        schema={"name": "ext__x__y", "input_schema": {"type": "object", "properties": {}}},
        source=ToolSource.BUILTIN, trust=Trust.BUILTIN,
    )
    reg.register(builtin_placeholder)                          # 既有 BUILTIN(占 ext__x__y 名)
    intruder = _ext_tool("ext__x__y", source=ToolSource.EXT, trust=Trust.TRUSTED)
    with pytest.raises(RuntimeError, match="reserved builtin"):
        reg.register(intruder)


def test_external_unprefixed_colliding_builtin_rejected_by_namespace():
    # 真实内置名(无前缀)被外部撞:forced-namespace 先行拦截(外部必须带前缀),同样 fail-loud。
    reg = ToolRegistry()
    reg.register(REGISTRY.get("read_file"))
    with pytest.raises(RuntimeError, match="namespace prefix"):
        reg.register(_ext_tool("read_file", source=ToolSource.MCP, trust=Trust.UNTRUSTED))


def test_external_missing_namespace_prefix_fails_loud():
    # forced-namespace：MCP/EXT/EMBEDDER 缺前缀 → 拒。
    reg = ToolRegistry()
    with pytest.raises(RuntimeError, match="namespace prefix"):
        reg.register(_ext_tool("bare_name", source=ToolSource.MCP))
    with pytest.raises(RuntimeError, match="namespace prefix"):
        reg.register(_ext_tool("bare_name", source=ToolSource.EXT))
    with pytest.raises(RuntimeError, match="namespace prefix"):
        reg.register(_ext_tool("bare_name", source=ToolSource.EMBEDDER))


def test_external_with_correct_prefix_registers():
    reg = ToolRegistry()
    reg.register(_ext_tool("mcp__srv__do", source=ToolSource.MCP))
    reg.register(_ext_tool("ext__plugin__do", source=ToolSource.EXT))
    reg.register(_ext_tool("embedder__do", source=ToolSource.EMBEDDER))
    assert set(reg.names()) == {"mcp__srv__do", "ext__plugin__do", "embedder__do"}


def test_non_trusted_duplicate_fails_loud():
    # override-only-TRUSTED：重名且 incoming 非 TRUSTED → 沿用 dup fail-loud。
    reg = ToolRegistry()
    reg.register(_ext_tool("ext__p__t", trust=Trust.TRUSTED))   # 先放一个非内置同名
    with pytest.raises(RuntimeError, match="duplicate tool name"):
        reg.register(_ext_tool("ext__p__t", trust=Trust.UNTRUSTED))


def test_trusted_duplicate_overrides():
    # TRUSTED 重名允许覆盖(替换)。
    reg = ToolRegistry()
    first = _ext_tool("ext__p__t", trust=Trust.UNTRUSTED)
    reg.register(first)
    second = _ext_tool("ext__p__t", trust=Trust.TRUSTED)
    reg.register(second)
    assert reg.get("ext__p__t") is second


def test_builtin_duplicate_still_fails_loud():
    # 内置重名(原 dup 语义)不受新规则放松:BUILTIN trust 非 TRUSTED → 仍炸。
    reg = ToolRegistry()
    t = REGISTRY.get("read_file")
    reg.register(t)
    with pytest.raises(RuntimeError, match="duplicate tool name"):
        reg.register(t)
