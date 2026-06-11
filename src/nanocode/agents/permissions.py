"""agents/permissions.py — profile 权限派生（docs/15 §11.3）。

子 agent 的有效权限从 parent + 自身 profile 派生,**子不得超过父**：
- permission mode：子继承父 mode（不放宽）；
- tools_deny：父 ∪ 子（deny 胜出、并集）；
- tools_allow：父 ∩ 子（allow 交集;None = 无 allow-list）；
- 后台子 agent：auto_deny_confirms（无 TTY,危险确认一律拒）；
- spawn：子默认不可 spawn 孙（can_spawn 取父子合取,且父显式允许才行）。

这是 capabilities/permissions.py 的 PermissionContext 的单一派生点（取代散落在
engine._build_sub_agent + subagents.config 的继承逻辑）。
"""

from __future__ import annotations

from dataclasses import replace

from .profile import AgentProfile, IsolationPolicy, PermissionProfile


def intersect_allow(parent: "set[str] | None", child: "set[str] | None") -> "set[str] | None":
    """allow-list 交集语义（None = 无约束/全部）。与 subagents.config._intersect_allowed 同语义。

    - 都 None → None；一方 None → 另一方；都有 → 交集（子绝不获得父没有的工具）。
    """
    if parent is None and child is None:
        return None
    if parent is None:
        return set(child) if child is not None else None
    if child is None:
        return set(parent)
    return set(parent) & set(child)


def derive_child_permission(parent: PermissionProfile, child: PermissionProfile,
                            *, background: bool) -> PermissionProfile:
    """派生子 PermissionProfile（§11.3）。mode 继承父;deny 并集;allow 交集;后台 auto-deny。"""
    return PermissionProfile(
        mode=parent.mode,  # 子继承父 mode（硬规则:子不得高于父）
        tools_allow=intersect_allow(parent.tools_allow, child.tools_allow),
        tools_deny=set(parent.tools_deny) | set(child.tools_deny),
        auto_deny_confirms=bool(background) or parent.auto_deny_confirms or child.auto_deny_confirms,
    )


def derive_child_profile(parent: AgentProfile, child: AgentProfile,
                         *, background: bool = False) -> AgentProfile:
    """从 parent + child profile 派生**有效**子 profile（子不得超过父）。

    返回一个新 AgentProfile（不改入参）：权限收窄 + 工具收窄 + isolation 收窄（默认不可 spawn 孙）。
    background=True：auto_deny_confirms + 不与父共享审批白名单（由 runtime spawn 处理,Phase 6）。
    """
    perm = derive_child_permission(parent.permission, child.permission, background=background)
    can_spawn = bool(parent.isolation.can_spawn and child.isolation.can_spawn)
    # 深度预算：取父子里更紧的 max_depth（None = 无显式限制）。
    depths = [d for d in (parent.isolation.max_depth, child.isolation.max_depth) if d is not None]
    max_depth = min(depths) if depths else None
    iso = IsolationPolicy(own_session=True, can_spawn=can_spawn, max_depth=max_depth)
    return replace(
        child,
        permission=perm,
        tools_allow=intersect_allow(parent.tools_allow, child.tools_allow),
        tools_deny=set(parent.tools_deny) | set(child.tools_deny),
        spawn_allow=(set(parent.spawn_allow) & set(child.spawn_allow)
                     if parent.spawn_allow is not None and child.spawn_allow is not None
                     else (child.spawn_allow if parent.spawn_allow is None else
                           (parent.spawn_allow if child.spawn_allow is None else set()))),
        isolation=iso,
    )


def effective_child_tools(parent: AgentProfile, child: AgentProfile, universe: set[str],
                          *, background: bool = False) -> set[str]:
    """派生子 agent 在 universe 上的**有效工具名集**（§11.3 + 永远剔 agent,除非 can_spawn）。"""
    derived = derive_child_profile(parent, child, background=background)
    return derived.effective_tool_names(universe)
