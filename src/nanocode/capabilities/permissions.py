"""capabilities/permissions.py — 不可变 PermissionContext（docs/15 §5/§13#6）。

PermissionEngine 今天持 live Agent back-ref（读 permission_mode / _plan_file_path /
_allowed_tool_names）。本模块提供一个**不可变** PermissionContext,从 AgentProfile + runtime 配置
构建,使权限决策不再耦合到 Agent god-class——PermissionEngine 已 duck-type 这三个属性,故
`PermissionEngine(ctx)` 直接可用,无需改 PermissionEngine 本体（additive 解耦）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..tools.permissions import Decision, PermissionEngine


@dataclass(frozen=True)
class PermissionContext:
    """工具派发的不可变权限上下文。

    - mode：permission mode（default/plan/acceptEdits/bypassPermissions/dontAsk）。
    - plan_file_path：plan 模式下唯一可编辑的计划文件路径（None = 非 plan）。
    - allowed_tool_names：子 agent call-time allowlist（None = 主 agent / 不约束）。

    PermissionEngine 读 `permission_mode` / `_plan_file_path` / `_allowed_tool_names`,故下面
    提供同名属性别名（保持与 PermissionEngine 的 duck-type 契约一致,零改动复用）。
    """

    mode: str = "default"
    plan_file_path: str | None = None
    allowed_tool_names: frozenset[str] | None = None

    # ── PermissionEngine duck-type 契约别名 ──
    @property
    def permission_mode(self) -> str:
        return self.mode

    @property
    def _plan_file_path(self) -> "str | None":
        return self.plan_file_path

    @property
    def _allowed_tool_names(self) -> "frozenset[str] | None":
        return self.allowed_tool_names

    @classmethod
    def from_profile(cls, profile, *, plan_file_path: str | None = None,
                     effective_tool_names: "set[str] | frozenset[str] | None" = None) -> "PermissionContext":
        """从 AgentProfile 构建（子 agent 的 allowlist = 有效工具名集;主 agent 传 None）。"""
        allow = None if effective_tool_names is None else frozenset(effective_tool_names)
        return cls(mode=profile.permission.mode, plan_file_path=plan_file_path, allowed_tool_names=allow)


def decide(ctx: PermissionContext, name: str, inp: dict) -> Decision:
    """据不可变上下文做一次工具派发决策（policy action + allowlist 标记）。纯决策,无副作用。"""
    return PermissionEngine(ctx).check(name, inp)
