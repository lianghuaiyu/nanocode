"""docs/15 Phase 0：AgentProfile 类型化 profile 契约（§10/§11.3）。"""

from nanocode.agents.profile import (
    AgentProfile, PermissionProfile, ContextProfile, IsolationPolicy,
)


UNIVERSE = {"read_file", "list_files", "grep_search", "write_file", "edit_file", "run_shell", "agent"}


def test_defaults_are_safe_subagent():
    p = AgentProfile(name="x")
    assert p.mode == "subagent"
    assert p.isolation.can_spawn is False
    assert p.hidden is False


def test_effective_tools_strips_agent_by_default():
    p = AgentProfile(name="general")            # allow=None → 全部,但永远剔 agent
    eff = p.effective_tool_names(UNIVERSE)
    assert "agent" not in eff
    assert "run_shell" in eff and "read_file" in eff


def test_effective_tools_allowlist_intersect():
    p = AgentProfile(name="explore", tools_allow={"read_file", "list_files", "grep_search"})
    eff = p.effective_tool_names(UNIVERSE)
    assert eff == {"read_file", "list_files", "grep_search"}


def test_effective_tools_deny_wins():
    p = AgentProfile(name="noshell", tools_deny={"run_shell"})
    eff = p.effective_tool_names(UNIVERSE)
    assert "run_shell" not in eff
    assert "agent" not in eff                   # 仍剔 agent


def test_can_spawn_keeps_agent_tool():
    p = AgentProfile(name="orchestrator", isolation=IsolationPolicy(can_spawn=True))
    eff = p.effective_tool_names(UNIVERSE)
    assert "agent" in eff


def test_is_spawnable_modes():
    assert AgentProfile(name="a", mode="subagent").is_spawnable()
    assert AgentProfile(name="b", mode="all").is_spawnable()
    assert not AgentProfile(name="c", mode="primary").is_spawnable()
    assert not AgentProfile(name="d", mode="system").is_spawnable()
    assert not AgentProfile(name="e", mode="subagent", hidden=True).is_spawnable()


def test_subpolicies_attach():
    p = AgentProfile(
        name="custom",
        permission=PermissionProfile(mode="plan", tools_deny={"run_shell"}, auto_deny_confirms=True),
        context=ContextProfile(codeintel=False, map_tokens=0),
    )
    assert p.permission.mode == "plan"
    assert p.permission.auto_deny_confirms is True
    assert p.context.codeintel is False
    assert p.context.map_tokens == 0
