"""capabilities/permissions.py — 不可变 PermissionContext（docs/15 §5/§13#6）。

不可变 PermissionContext + 纯决策函数 decide()——权限裁决的单一基底（docs/16 #7b）。
PermissionEngine（tools/permissions.py）持 live Agent back-ref，但其 check() 现在只做一件事：
把 live 属性（permission_mode / _plan_file_path / _allowed_tool_names）快照成 PermissionContext
再调 decide()。任何替代宿主（SDK / AppServer / profile 驱动的 spawn）直接构建 ctx 即可。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..tools.permissions import Decision, allowlist_blocks, check_permission


@dataclass(frozen=True)
class PermissionContext:
    """工具派发的不可变权限上下文。

    - mode：permission mode（default/plan/acceptEdits/bypassPermissions/dontAsk）。
    - plan_file_path：plan 模式下唯一可编辑的计划文件路径（None = 非 plan）。
    - allowed_tool_names：子 agent call-time allowlist（None = 主 agent / 不约束）。
    - interactive：是否存在人面审批通道（无 → request_approval 转 deny，fail-closed）。
    - is_subagent / is_background / is_hook：执行身份（docs/19 §4.1，runtime 注入，非模型）。
    - workspace_roots：可写工作区根（诊断 / 未来 typed action 用）。
    - approval_mode：审批姿态（on-request / explicit）。

    PermissionEngine 读 `permission_mode` / `_plan_file_path` / `_allowed_tool_names`,故下面
    提供同名属性别名（保持与 PermissionEngine 的 duck-type 契约一致,零改动复用）。
    """

    mode: str = "default"
    plan_file_path: str | None = None
    allowed_tool_names: frozenset[str] | None = None
    interactive: bool = True
    is_subagent: bool = False
    is_background: bool = False
    is_hook: bool = False
    workspace_roots: tuple = ()
    approval_mode: str = "default"

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
    """据不可变上下文做一次工具派发决策（policy action + allowlist 标记）。纯决策,无副作用。

    docs/16 #7b：这是权限决策的**基底**——PermissionEngine.check 也经此（live agent 属性
    每次 check 快照成 PermissionContext 再裁决），决策逻辑单点化、不再依赖 Agent god-class。

    docs/19 review：非交互上下文（``ctx.interactive`` False）无法新审批 → ``confirm`` 收敛为
    ``deny``（fail-closed）。Agent 路径默认 interactive=True，仍由 confirm_fn 往返（行为不变）；
    替代宿主（SDK/AppServer）构建 ``interactive=False`` 的 ctx 即在基底层 fail-closed。"""
    policy = check_permission(name, inp, ctx.permission_mode, ctx._plan_file_path)
    action = policy["action"]
    message = policy.get("message", "")
    if action == "confirm" and not ctx.interactive:
        action = "deny"
        message = message or "approval required but context is non-interactive"
    return Decision(
        action=action,
        message=message,
        allowlist_blocked=allowlist_blocks(name, ctx._allowed_tool_names),
    )
