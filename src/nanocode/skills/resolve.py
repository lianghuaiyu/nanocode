"""技能解析与执行：参数替换、按名取技能、生成系统提示词中的技能清单。"""

from __future__ import annotations

from .discovery import discover_skills, SkillDefinition


# ─── Resolution ─────────────────────────────────────────────


def get_skill_by_name(name: str) -> SkillDefinition | None:
    for s in discover_skills():
        if s.name == name:
            return s
    return None


def resolve_skill_prompt(skill: SkillDefinition, args: str) -> str:
    import re
    prompt = skill.prompt_template
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", args, prompt)
    prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)
    return prompt


def execute_skill(
    skill_name: str, args: str
) -> dict | None:
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None
    return {
        "prompt": resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
    }


# ─── System prompt section ──────────────────────────────────


def build_skill_descriptions() -> str:
    skills = discover_skills()
    if not skills:
        return ""

    lines = ["# Available Skills", ""]
    invocable = [s for s in skills if s.user_invocable]
    auto_only = [s for s in skills if not s.user_invocable]

    if invocable:
        lines.append("User-invocable skills (user types /<name> to invoke):")
        for s in invocable:
            lines.append(f"- **/{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    if auto_only:
        lines.append("Auto-invocable skills (use the skill tool when appropriate):")
        for s in auto_only:
            lines.append(f"- **{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    lines.append("To invoke a skill programmatically, use the `skill` tool with the skill name and optional arguments.")
    return "\n".join(lines)
