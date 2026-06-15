"""docs/16 #5：ToolSpec 单一真相源 + ToolHost typed port。

防漂移锚点：registry.tool_definitions 与 execute._HANDLERS 均从 spec.TOOLS 派生——
两份注册表不可能再各自漂移；Agent 必须结构性满足 ToolHost（router.dispatch 的依赖面）。
"""

from nanocode.agent.engine import Agent
from nanocode.capabilities import ToolHost
from nanocode.tools import (
    CONCURRENCY_SAFE_TOOLS, TOOLS, coding_tools, read_only_tools, tool_definitions,
)
from nanocode.tools.execute import _HANDLERS


def test_tool_definitions_derived_from_spec_in_order():
    assert tool_definitions == [s.schema for s in TOOLS.values()]
    names = [s.name for s in TOOLS.values()]
    assert len(names) == len(set(names))                       # 重名构造期即炸（spec.py RuntimeError）
    # 字面顺序锚定（= 发给 API 的顺序；改动须有意为之）
    assert names[:8] == ["read_file", "write_file", "edit_file", "list_files", "grep_search",
                         "run_shell", "skill", "web_fetch"]
    assert "sandbox_shell" not in names                         # docs/19：public sandbox_shell 已删


def test_handlers_derived_from_spec():
    assert _HANDLERS == {n: s.run for n, s in TOOLS.items() if s.run is not None}
    # host-routed 工具绝不进 handler 表（它们经 CapabilityRouter 分发）
    # docs/19：run_shell 现也是 host-routed（经 SandboxManager 执行，run=None）。
    for host_routed in ("run_shell", "agent", "skill", "enter_plan_mode", "exit_plan_mode",
                        "task_list", "task_output", "task_stop", "tool_search"):
        assert host_routed in TOOLS and TOOLS[host_routed].run is None
        assert host_routed not in _HANDLERS


def test_concurrency_safe_mirrors_permissions_classification():
    # 真相源在 permissions（安全相邻分类）；spec 只镜像，绝不另起炉灶。
    assert {n for n, s in TOOLS.items() if s.concurrency_safe} == set(CONCURRENCY_SAFE_TOOLS)


def test_deferred_flag_mirrors_schema():
    for n, s in TOOLS.items():
        assert s.deferred == bool(s.schema.get("deferred")), n


def test_bundles_are_known_tools_and_disjoint_from_spawn():
    known = set(TOOLS)
    assert set(read_only_tools()) <= known and set(coding_tools()) <= known
    assert set(read_only_tools()) <= set(coding_tools())
    for bundle in (read_only_tools(), coding_tools()):
        assert "agent" not in bundle                            # spawn 永不进 bundle（fail-closed 语义）
    for n in read_only_tools():
        assert n not in ("write_file", "edit_file", "run_shell", "sandbox_shell")


def test_agent_structurally_satisfies_toolhost():
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    assert isinstance(a, ToolHost)                              # runtime_checkable 结构检查
