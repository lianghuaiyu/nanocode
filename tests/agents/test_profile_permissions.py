"""docs/15 Phase 5 §11.3：profile 权限派生（child≤parent）+ registry profile 解析。"""

from nanocode.agents.permissions import (
    derive_child_profile, derive_child_permission, effective_child_tools, intersect_allow,
)
from nanocode.agents.profile import AgentProfile, IsolationPolicy, PermissionProfile
from nanocode.agents.registry import build_profile, build_primary_profile, list_spawnable_profiles

UNIVERSE = {"read_file", "list_files", "grep_search", "write_file", "edit_file", "run_shell", "agent"}


# ─── intersect / deny 代数 ───────────────────────────────────────────────────
def test_intersect_allow_semantics():
    assert intersect_allow(None, None) is None
    assert intersect_allow(None, {"a"}) == {"a"}
    assert intersect_allow({"a", "b"}, None) == {"a", "b"}
    assert intersect_allow({"a", "b"}, {"b", "c"}) == {"b"}


def test_derive_permission_mode_inherited_deny_union_allow_intersect():
    parent = PermissionProfile(mode="acceptEdits", tools_allow={"read_file", "run_shell", "write_file"},
                               tools_deny={"web_fetch"})
    child = PermissionProfile(mode="default", tools_allow={"read_file", "run_shell"},
                              tools_deny={"run_shell"})
    d = derive_child_permission(parent, child, background=False)
    assert d.mode == "acceptEdits"                       # 子继承父 mode
    assert d.tools_allow == {"read_file", "run_shell"}    # 交集
    assert d.tools_deny == {"web_fetch", "run_shell"}     # 并集


def test_background_forces_auto_deny():
    d = derive_child_permission(PermissionProfile(), PermissionProfile(), background=True)
    assert d.auto_deny_confirms is True


def test_child_cannot_exceed_parent_tools():
    parent = AgentProfile(name="p", tools_allow={"read_file", "grep_search"})
    child = AgentProfile(name="c", tools_allow=None)      # 子声明无约束,但父已收窄
    eff = effective_child_tools(parent, child, UNIVERSE)
    assert eff == {"read_file", "grep_search"}            # 子绝不获得父没有的工具
    assert "run_shell" not in eff and "agent" not in eff


def test_child_cannot_spawn_unless_both_allow():
    parent = AgentProfile(name="p", isolation=IsolationPolicy(can_spawn=True))
    child_no = AgentProfile(name="c", isolation=IsolationPolicy(can_spawn=False))
    assert derive_child_profile(parent, child_no).isolation.can_spawn is False
    child_yes = AgentProfile(name="c2", isolation=IsolationPolicy(can_spawn=True))
    assert derive_child_profile(parent, child_yes).isolation.can_spawn is True
    # 父不允许 spawn → 子一定不能
    parent_no = AgentProfile(name="pn", isolation=IsolationPolicy(can_spawn=False))
    assert derive_child_profile(parent_no, child_yes).isolation.can_spawn is False


def test_derive_does_not_mutate_inputs():
    parent = AgentProfile(name="p", tools_deny={"x"})
    child = AgentProfile(name="c", tools_deny={"y"})
    derive_child_profile(parent, child)
    assert parent.tools_deny == {"x"} and child.tools_deny == {"y"}   # 入参不变


def test_max_depth_takes_tighter():
    parent = AgentProfile(name="p", isolation=IsolationPolicy(can_spawn=True, max_depth=3))
    child = AgentProfile(name="c", isolation=IsolationPolicy(can_spawn=True, max_depth=1))
    assert derive_child_profile(parent, child).isolation.max_depth == 1


# ─── registry：从既有 config 解析 typed profile ──────────────────────────────
def test_build_profile_explore_is_readonly_subagent():
    p = build_profile("explore")
    assert p.mode == "subagent"
    assert p.tools_allow == {"read_file", "list_files", "grep_search"}
    assert "agent" not in p.effective_tool_names(UNIVERSE)   # 子不 spawn 孙
    assert p.is_spawnable()


def test_build_profile_general_unrestricted_but_no_agent():
    p = build_profile("general")
    assert p.mode == "subagent"
    eff = p.effective_tool_names(UNIVERSE)
    assert "run_shell" in eff and "agent" not in eff


def test_build_primary_can_spawn():
    p = build_primary_profile(model="claude-x")
    assert p.mode == "primary"
    assert "agent" in p.effective_tool_names(UNIVERSE)       # primary 可 spawn
    assert not p.is_spawnable()                              # primary 不作为被 spawn 的目标


def test_list_spawnable_profiles_includes_builtins():
    names = {p.name for p in list_spawnable_profiles()}
    assert {"explore", "plan", "general"} <= names
