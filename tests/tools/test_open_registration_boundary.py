"""docs/24 Phase 4 边界测试：per-agent overlay + 开放注册（MCP/扩展/嵌入者）。

命根子不变量（§8.1 / §4.3 / §4.5）：
- 外部工具（MCP/EXT/EMBEDDER）只进 **per-agent** overlay，绝不写全局 REGISTRY（跨 agent 不串）。
- UNTRUSTED 外部工具经 _granted_capabilities 得 ∅ → ToolContext 所有能力槽 None（够不到 fs/exec/
  spawn/memory）。
- 外部工具撞内置名 fail-loud；外部强制 namespace 前缀。
- 外部工具仍先过 dispatch 咽喉点（allowlist + 校验）再执行。
- register 同源覆盖收紧：跨源不得 last-writer-wins。
"""
import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.extensions.host import ExtensionHost
from nanocode.tools import REGISTRY
from nanocode.tools.registry import ToolRegistry
from nanocode.tools.spec import Tool
from nanocode.tools.types import Capability, ToolSource, Trust


_CAP_SLOTS = ("fs_read", "fs_write", "fs_list", "exec", "tasks", "runs",
              "memory", "spawn", "set_mode")


def _ext_tool(name, *, source, trust=Trust.UNTRUSTED, needs=frozenset(), run=None):
    return Tool(
        schema={"name": name, "description": "d",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
        run=run, source=source, trust=trust, needs=needs,
    )


# ─── overlay 隔离：外部工具不污染全局 / 跨 agent 不串 ─────────────────────────────

def test_overlay_with_empty_extra_is_equivalent_to_global():
    reg = REGISTRY.overlay([])
    assert reg.names() == REGISTRY.names()
    assert reg is not REGISTRY
    # activation 状态是 per-registry 的独立可变态（不共享全局集）。
    assert reg._activated is not REGISTRY._activated


def test_overlay_does_not_mutate_global_registry():
    before = set(REGISTRY.names())
    reg = REGISTRY.overlay([_ext_tool("mcp__srv__do", source=ToolSource.MCP)])
    assert "mcp__srv__do" in reg.names()
    assert "mcp__srv__do" not in REGISTRY.names()
    assert set(REGISTRY.names()) == before


def test_two_agents_do_not_share_external_tools():
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    b = Agent(api_key="test", permission_mode="bypassPermissions")
    a._registry.register(_ext_tool("mcp__srv__do", source=ToolSource.MCP))
    assert a._registry.get("mcp__srv__do") is not None
    assert b._registry.get("mcp__srv__do") is None        # 跨 agent 不串
    assert REGISTRY.get("mcp__srv__do") is None            # 全局未被污染


def test_per_registry_activation_is_independent():
    a = ToolRegistry.from_builtins([REGISTRY.get(n) for n in REGISTRY.names()])
    b = a.overlay([])
    a.activate("enter_plan_mode")
    assert "enter_plan_mode" in a._activated
    assert "enter_plan_mode" not in b._activated           # overlay 不继承激活态


# ─── UNTRUSTED → 空 ctx（够不到内核能力面）─────────────────────────────────────

def test_untrusted_external_tool_gets_all_none_ctx():
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    seen = {}

    def _run(ctx, inp):
        seen["ctx"] = ctx
        return "ran"

    # 即便声明一堆敏感能力，UNTRUSTED 策略夹成 ∅ → ctx 全 None。
    a._registry.register(_ext_tool(
        "ext__demo__x", source=ToolSource.EXT, trust=Trust.UNTRUSTED,
        needs=frozenset({Capability.FS_READ, Capability.EXEC, Capability.MEMORY,
                         Capability.SPAWN, Capability.TASKS, Capability.SESSION_READ}),
        run=_run))
    out = asyncio.run(a._execute_tool_call("ext__demo__x", {}))
    assert out == "ran"
    ctx = seen["ctx"]
    for slot in _CAP_SLOTS:
        assert getattr(ctx, slot) is None, slot
    # ToolContext 仍无任何字段通向 raw 内核。
    for attr in ("agent", "_session_mgr", "lease"):
        assert not hasattr(ctx, attr)


# ─── 外部撞内置名 fail-loud + forced namespace ───────────────────────────────────

def test_external_cannot_shadow_builtin_name():
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    # read_file 是内置保留名：外部缺前缀先被 forced-namespace 拦（同样 fail-loud）。
    with pytest.raises(RuntimeError, match="namespace prefix"):
        a._registry.register(_ext_tool("read_file", source=ToolSource.MCP))


def test_external_missing_prefix_fails_loud():
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    for source in (ToolSource.MCP, ToolSource.EXT, ToolSource.EMBEDDER):
        with pytest.raises(RuntimeError, match="namespace prefix"):
            a._registry.register(_ext_tool("bare", source=source))


# ─── register 同源覆盖收紧（跨源不得 last-writer-wins）──────────────────────────

def test_cross_source_override_rejected_by_namespace_partition():
    # forced-namespace 已按 source 分区名字空间：EXT 工具不可能取 mcp__ 名 → 跨源抢注在更早
    # 一层即被 namespace 规则拦下（fail-loud）。register 的同源约束（incoming.source is
    # existing.source）是其后的 defense-in-depth：因 namespace 分区，外部源跨源同名已不可达，
    # 该约束确保即便将来放宽 namespace 也不会 last-writer-wins。
    reg = ToolRegistry()
    reg.register(_ext_tool("mcp__srv__t", source=ToolSource.MCP, trust=Trust.UNTRUSTED))
    with pytest.raises(RuntimeError, match="namespace prefix"):
        reg.register(Tool(
            schema={"name": "mcp__srv__t", "input_schema": {"type": "object", "properties": {}}},
            source=ToolSource.EXT, trust=Trust.TRUSTED))


def test_same_source_trusted_override_allowed():
    reg = ToolRegistry()
    reg.register(_ext_tool("ext__p__t", source=ToolSource.EXT, trust=Trust.UNTRUSTED))
    second = _ext_tool("ext__p__t", source=ToolSource.EXT, trust=Trust.TRUSTED)
    reg.register(second)
    assert reg.get("ext__p__t") is second


# ─── MCP 经 source 路由（非前缀旁路）─────────────────────────────────────────────

def test_mcp_routed_by_source_not_prefix():
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    a._registry.register(_ext_tool("mcp__srv__do", source=ToolSource.MCP))
    called = {}

    async def _fake_call(name, inp):
        called["name"] = name
        called["inp"] = inp
        return "mcp-result"

    a._mcp_manager.call_tool = _fake_call
    out = asyncio.run(a._run_real_tool("mcp__srv__do", {"q": 1}))
    assert out == "mcp-result"
    assert called == {"name": "mcp__srv__do", "inp": {"q": 1}}


# ─── 扩展 register_tool 端到端（经权限咽喉点后执行，ctx 全 None）─────────────────

def test_extension_register_tool_end_to_end():
    host = ExtensionHost(manifests=[])
    host._activated = True
    seen = {}

    async def _h(inp, ctx):
        seen["ctx"] = ctx
        return "ext-ok:" + str(inp.get("y"))

    host.registry.add_tool(
        "frob",
        {"name": "frob",
         "input_schema": {"type": "object", "properties": {"y": {"type": "string"}},
                          "additionalProperties": False}},
        _h, needs=frozenset({Capability.MEMORY}), extension_id="memev")

    tools = host.tool_contributions()
    assert tools[0].name == "ext__memev__frob"
    assert tools[0].source is ToolSource.EXT and tools[0].trust is Trust.UNTRUSTED

    a = Agent(api_key="test", permission_mode="bypassPermissions")
    for t in tools:
        a._registry.register(t)
    a.tools = a._registry.schemas()
    out = asyncio.run(a._execute_tool_call("ext__memev__frob", {"y": "z"}))
    assert out == "ext-ok:z"
    for slot in _CAP_SLOTS:                                # UNTRUSTED → 空 ctx
        assert getattr(seen["ctx"], slot) is None, slot


def test_extension_tool_still_passes_through_allowlist_chokepoint():
    # 外部工具仍先过 dispatch 咽喉点：子 agent allowlist 不含该名 → fail-closed 拦截。
    host = ExtensionHost(manifests=[])
    host._activated = True

    async def _h(inp, ctx):
        return "should-not-run"

    host.registry.add_tool(
        "frob", {"name": "frob", "input_schema": {"type": "object", "properties": {}}},
        _h, needs=frozenset(), extension_id="memev")

    a = Agent(api_key="test", permission_mode="bypassPermissions",
              is_sub_agent=True, allowed_tool_names={"read_file"})
    for t in host.tool_contributions():
        a._registry.register(t)
    out = asyncio.run(a._execute_tool_call("ext__memev__frob", {}))
    assert "not permitted" in out


# ─── 嵌入者 AgentConfig.tools 注入口 ─────────────────────────────────────────────

def test_embedder_tools_registered_with_namespace_and_untrusted_ctx():
    seen = {}

    def _run(ctx, inp):
        seen["ctx"] = ctx
        return "emb:" + str(inp.get("x"))

    t = Tool(
        schema={"name": "do_x", "description": "d",
                "input_schema": {"type": "object", "properties": {"x": {"type": "string"}},
                                 "additionalProperties": False}},
        run=_run, trust=Trust.UNTRUSTED,
        needs=frozenset({Capability.FS_WRITE, Capability.EXEC}))
    a = Agent(api_key="test", permission_mode="bypassPermissions", embedder_tools=[t])
    assert "embedder__do_x" in a._registry.names()
    assert REGISTRY.get("embedder__do_x") is None           # 全局未污染
    out = asyncio.run(a._execute_tool_call("embedder__do_x", {"x": "hi"}))
    assert out == "emb:hi"
    for slot in _CAP_SLOTS:
        assert getattr(seen["ctx"], slot) is None, slot


def test_embedder_config_passes_tools_through_build_agent():
    from nanocode.runtime.facade import AgentConfig
    t = Tool(
        schema={"name": "embedder__ping", "input_schema": {"type": "object", "properties": {}}},
        run=lambda ctx, inp: "pong", trust=Trust.UNTRUSTED)
    agent = AgentConfig(api_key="test", tools=[t]).build_agent()
    assert "embedder__ping" in agent._registry.names()
