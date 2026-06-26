"""runtime/skill_service.py — runtime 拥有的 skill 解析 + hook 安装服务（docs/23 Phase 5）。

把 USER `/skill` 调用的解析（get_skill_by_name / execute_skill / resolve_skill_prompt）与
hook 安装（agent._register_skill_hooks）从 RuntimeThread.invoke_skill 的内联体抽到 runtime
拥有的服务里，使 facade 不再内联 skill registry helper、也不再直接 reach 进 agent 私有 hook
安装面。本服务是纯提取——行为与原内联实现逐字等价，不做重设计。

不在范围内：模型面 `skill` 工具路径（CapabilityRouter → host.execute_skill_tool →
Agent._execute_skill_tool）独立保留，不经本服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .facade import SkillInvocation


@dataclass(frozen=True)
class ResolvedSkillInvocation:
    """resolve_user_invocation 的结果：对外的 SkillInvocation + 触发 hook 安装所需的 skill。

    skill 仅在 skill 存在且 user_invocable（即原内联体会走到 hook 安装分支）时非 None；
    `not skill or not user_invocable` 时为 None——与原内联体一致：此分支直接返回
    handled=False 且不安装 hook。
    """

    invocation: SkillInvocation
    skill: Any | None = None


class SkillRuntimeService:
    """runtime 拥有的 skill 服务：解析 USER `/skill` 调用，并安装其 frontmatter hooks。"""

    def resolve_user_invocation(self, name: str, args: str) -> ResolvedSkillInvocation:
        # call-time import：与原 invoke_skill 内联体一致，从 nanocode.skills 取当前绑定
        # （测试以 monkeypatch nanocode.skills.* 注入桩，必须在调用时解析才能命中）。
        from ..skills import execute_skill, get_skill_by_name, resolve_skill_prompt

        skill = get_skill_by_name(name)
        if not skill or not skill.user_invocable:
            return ResolvedSkillInvocation(SkillInvocation(handled=False))
        if skill.context == "fork":
            result = execute_skill(skill.name, args)
            if not result:
                return ResolvedSkillInvocation(
                    SkillInvocation(handled=True, error=f"Unknown skill: {skill.name}"),
                    skill=skill,
                )
            return ResolvedSkillInvocation(
                SkillInvocation(
                    handled=True,
                    notice=f"Invoking skill: {skill.name}",
                    prompt=f'Use the skill tool to invoke "{skill.name}" with args: {args or "(none)"}',
                ),
                skill=skill,
            )
        return ResolvedSkillInvocation(
            SkillInvocation(
                handled=True,
                notice=f"Invoking skill: {skill.name}",
                prompt=resolve_skill_prompt(skill, args),
            ),
            skill=skill,
        )

    def install_hooks(self, agent, skill) -> None:
        """把 skill frontmatter hooks 安装到 agent（仅当存在 hooks），经 runtime-private 路径。

        facade 不再直接调用 agent._register_skill_hooks——hook 安装由本服务持有。
        """
        if getattr(skill, "hooks", None):
            agent._register_skill_hooks(skill)
