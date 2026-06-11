"""agents/registry.py — AgentProfile 发现 / 解析（docs/15 §10）。

把 subagents/config.py 的 dict 配置（_resolve_effective + trust gate + extends 收窄 + _filter_tools）
解析成 typed AgentProfile。迁移期作 thin shim 复用既有发现/收窄逻辑（synth 允许）;后续把
_resolve_effective 算法整体 port 进来后,subagents/config.py 即可退役。

built-in profiles（§10）：
- build：主 write-capable primary；plan：write/shell 受限 primary；
- explore：只读 subagent；general/coder：write-capable subagent；
- system（compaction/title/summary/memory curators）：hidden,不向模型暴露为可 spawn。
"""

from __future__ import annotations

from .profile import (
    AgentProfile, ContextProfile, IsolationPolicy, PermissionProfile,
)


def build_profile(agent_type: str) -> AgentProfile:
    """从 subagents.config.get_sub_agent_config 解析出 typed AgentProfile（subagent mode）。

    tools_allow = 有效 allowed-name 集（None = 无 allow-list）;tools_deny = disallowed_names;
    model/max_turns/timeout/source/prompt 透传。isolation.can_spawn=False（子不 spawn 孙）。
    """
    from ..subagents.config import get_sub_agent_config, RESERVED_AGENT_TYPES
    cfg = get_sub_agent_config(agent_type)
    allowed = cfg.get("allowed_names")
    return AgentProfile(
        name=agent_type,
        description=cfg.get("description", "") if isinstance(cfg.get("description"), str) else "",
        mode="system" if agent_type in RESERVED_AGENT_TYPES else "subagent",
        prompt=cfg.get("system_prompt", ""),
        model=cfg.get("model"),
        max_turns=cfg.get("max_turns"),
        timeout_ms=cfg.get("timeout_ms"),
        tools_allow=set(allowed) if allowed is not None else None,
        tools_deny=set(cfg.get("disallowed_names") or set()),
        permission=PermissionProfile(),               # spawn 时从父派生（agents.permissions）
        context=ContextProfile(),
        isolation=IsolationPolicy(own_session=True, can_spawn=False),
        source=cfg.get("source"),
        hidden=agent_type in RESERVED_AGENT_TYPES,
    )


def list_spawnable_profiles() -> list[AgentProfile]:
    """可经 agent 工具 spawn 的 profile（explore/plan/general + 自定义;剔保留 system 类型）。"""
    from ..subagents.config import get_available_agent_types
    return [build_profile(t["name"]) for t in get_available_agent_types()]


# ─── built-in primary / read-only profiles（§10）──────────────────────────────
def build_primary_profile(*, model: str | None = None) -> AgentProfile:
    """主 write-capable primary agent（build）。无 allow-list（全部工具,含 agent 可 spawn 子）。"""
    return AgentProfile(
        name="build", description="Primary write-capable coding agent",
        mode="primary", model=model,
        tools_allow=None, tools_deny=set(),
        permission=PermissionProfile(mode="default"),
        context=ContextProfile(),
        isolation=IsolationPolicy(own_session=True, can_spawn=True),
    )


def build_plan_profile(*, model: str | None = None) -> AgentProfile:
    """plan primary：read-only(write/shell 受限),用于规划。"""
    return AgentProfile(
        name="plan", description="Read-only planning primary",
        mode="primary", model=model,
        tools_allow={"read_file", "list_files", "grep_search", "write_file", "edit_file"},
        tools_deny={"run_shell", "sandbox_shell"},
        permission=PermissionProfile(mode="plan"),
        context=ContextProfile(),
        isolation=IsolationPolicy(own_session=True, can_spawn=True),
    )
