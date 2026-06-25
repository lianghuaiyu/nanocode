"""docs/24 Phase 1：ToolRegistry 单一真相源 + ToolHost typed port。

防漂移锚点：合一后的 ToolRegistry 是工具真相源的唯一门面——schemas()/get()/deferred_names()
不可能再各自漂移；Agent 必须结构性满足 ToolHost（router.dispatch 的依赖面）。
"""

from nanocode.agent.engine import Agent
from nanocode.capabilities import ToolHost
from nanocode.tools import (
    CONCURRENCY_SAFE_TOOLS, REGISTRY, Tool, coding_tools, read_only_tools,
)
from nanocode.tools.types import Capability, ToolSource, Trust


def test_schemas_in_canonical_order():
    names = REGISTRY.names()
    assert len(names) == len(set(names))                       # 重名构造期即炸（registry.register fail-loud）
    # schemas() 保序，逐项 = 各工具 schema（原 tool_definitions 字面顺序）
    assert [s["name"] for s in REGISTRY.schemas()] == names
    # 字面顺序锚定（= 发给 API 的顺序；改动须有意为之）
    assert names[:8] == ["read_file", "write_file", "edit_file", "list_files", "grep_search",
                         "run_shell", "skill", "web_fetch"]
    assert "sandbox_shell" not in names                         # docs/19：public sandbox_shell 已删


def test_schemas_preserve_deferred_key():
    # schemas()（无 active）保留 deferred 键，等价旧 tool_definitions。
    by_name = {t.schema["name"]: t for t in (REGISTRY.get(n) for n in REGISTRY.names())}
    for s in REGISTRY.schemas():
        tool = REGISTRY.get(s["name"])
        assert s.get("deferred", False) == tool.deferred


def test_handlers_are_run_non_none_subset():
    # handler 子集 = run 非 None 的工具（execute.py 经 REGISTRY.get(name).run 派发）。
    handler_names = {n for n in REGISTRY.names() if REGISTRY.get(n).run is not None}
    # docs/24 Phase 3：host-routed 工具现也有自包含 run（经 ctx 能力把手），但**不**经
    # execute.py 通用 handler —— CapabilityRouter 在 hook 段之前/REAL 段按工具名分发它们。
    # 真正无 run 的只有 tool_search（execute.py 专用 deferred-激活分支）。
    for host_routed in ("run_shell", "agent", "skill", "enter_plan_mode", "exit_plan_mode",
                        "task_list", "task_output", "task_stop", "memory",
                        "run_list", "run_status", "run_output", "run_cancel", "run_send",
                        "get_subagent_result"):
        tool = REGISTRY.get(host_routed)
        assert tool is not None and tool.run is not None
        assert host_routed in handler_names
    # tool_search 无 module-level run（execute.py 专用分支激活 deferred 工具）。
    assert REGISTRY.get("tool_search").run is None


def test_concurrency_safe_mirrors_permissions_classification():
    # 真相源在 permissions（安全相邻分类）；Tool 只镜像，绝不另起炉灶。
    safe = {n for n in REGISTRY.names() if REGISTRY.get(n).concurrency_safe}
    assert safe == set(CONCURRENCY_SAFE_TOOLS)


def test_deferred_flag_mirrors_schema():
    for n in REGISTRY.names():
        s = REGISTRY.get(n)
        assert s.deferred == bool(s.schema.get("deferred")), n


def test_bundles_are_known_tools_and_disjoint_from_spawn():
    known = set(REGISTRY.names())
    assert set(read_only_tools()) <= known and set(coding_tools()) <= known
    assert set(read_only_tools()) <= set(coding_tools())
    for bundle in (read_only_tools(), coding_tools()):
        assert "agent" not in bundle                            # spawn 永不进 bundle（fail-closed 语义）
    for n in read_only_tools():
        assert n not in ("write_file", "edit_file", "run_shell", "sandbox_shell")


def test_active_schemas_strips_deferred_key():
    # active_schemas()（旧 get_active_tool_definitions）剔未激活 deferred + strip 'deferred' 键。
    REGISTRY.reset_activated()
    active = REGISTRY.active_schemas()
    assert all("deferred" not in s for s in active)
    # 未激活的 deferred 工具不出现在 active 集
    deferred = set(REGISTRY.deferred_names())
    assert deferred and not (deferred & {s["name"] for s in active})


def test_builtin_tools_declare_builtin_trust_and_source():
    # docs/24 Phase 1 惰性元数据：内置工具一律 BUILTIN trust/source。
    for n in REGISTRY.names():
        t = REGISTRY.get(n)
        assert t.trust is Trust.BUILTIN
        assert t.source is ToolSource.BUILTIN
        assert isinstance(t.needs, frozenset)


def test_needs_declarations_for_host_routed():
    # run_shell 前台需 EXEC、后台需 TASKS（spawn_shell）—— 声明二者。
    assert REGISTRY.get("run_shell").needs == frozenset({Capability.EXEC, Capability.TASKS})
    assert REGISTRY.get("agent").needs == frozenset({Capability.SPAWN})
    assert REGISTRY.get("memory").needs == frozenset({Capability.MEMORY})
    assert REGISTRY.get("task_list").needs == frozenset({Capability.TASKS})
    assert REGISTRY.get("enter_plan_mode").needs == frozenset({Capability.SET_MODE})
    assert REGISTRY.get("read_file").needs == frozenset({Capability.FS_READ})
    assert REGISTRY.get("write_file").needs == frozenset({Capability.FS_WRITE})


def test_register_duplicate_fails_loud():
    import pytest
    from nanocode.tools.registry import ToolRegistry
    reg = ToolRegistry()
    t = REGISTRY.get("read_file")
    reg.register(t)
    with pytest.raises(RuntimeError, match="duplicate tool name"):
        reg.register(t)


def test_agent_structurally_satisfies_toolhost():
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    assert isinstance(a, ToolHost)                              # runtime_checkable 结构检查
