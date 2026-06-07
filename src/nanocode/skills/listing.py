"""技能清单的渐进披露：预算化清单文本 + 差分播报 + body 投递辅助（贴 CC）。"""
from __future__ import annotations

from .discovery import discover_skills, SkillDefinition

SKILL_LISTING_PER_ITEM = 250
SKILL_LISTING_CHAR_BUDGET = 8000

SKILL_PROMPT_GUIDANCE = (
    "# Skills\n\n"
    "Available skills are announced in <system-reminder> messages as they become "
    "relevant. To run one, call the `skill` tool with its name (and optional args); "
    "its full instructions are then provided as a follow-up message. Users may also "
    "invoke a skill by typing /<name>."
)


def _line(s: SkillDefinition, per_item: int, truncate: bool = True) -> str:
    slash = "/" if s.user_invocable else ""
    desc = s.description or ""
    if s.when_to_use:
        desc = f"{desc} — {s.when_to_use}" if desc else s.when_to_use
    if truncate and len(desc) > per_item:
        desc = desc[:per_item - 1].rstrip() + "…"
    return f"- **{slash}{s.name}**: {desc}" if desc else f"- **{slash}{s.name}**"


def build_skill_listing(skills, char_budget: int = SKILL_LISTING_CHAR_BUDGET,
                        per_item: int = SKILL_LISTING_PER_ITEM) -> str:
    if not skills:
        return ""
    header = "The following skills are available (invoke via the `skill` tool):"
    full = "\n".join([header, *[_line(s, per_item) for s in skills]])
    if len(full) <= char_budget:
        return full
    names = [f"- **{('/' if s.user_invocable else '')}{s.name}**" for s in skills]
    return "\n".join([header, *names])


def visible_model_skills(skills, activated):
    """过滤出模型可自动调用的清单：disable_model_invocation 排除；有 paths 且未激活则排除。"""
    out = []
    for s in skills:
        if getattr(s, "disable_model_invocation", False):
            continue
        if getattr(s, "paths", None) and s.name not in activated:
            continue
        out.append(s)
    return out


def skill_listing_delta(sent_names: set[str],
                        activated=frozenset(),
                        char_budget: int = SKILL_LISTING_CHAR_BUDGET):
    """返回 (<system-reminder> 包裹的清单文本 | None, 新播报的名字列表)。"""
    new = [s for s in visible_model_skills(discover_skills(), activated)
           if s.name not in sent_names]
    if not new:
        return None, []
    text = build_skill_listing(new, char_budget=char_budget)
    if not text:
        return None, []
    return f"<system-reminder>\n{text}\n</system-reminder>", [s.name for s in new]


def render_skill_body_message(name: str, body: str) -> dict:
    return {"role": "user", "content": f"<command-name>{name}</command-name>\n\n{body}"}


def append_to_last_user(messages: list, text: str) -> None:
    last = messages[-1] if messages else None
    if last and last.get("role") == "user":
        content = last.get("content", "")
        if isinstance(content, str):
            last["content"] = (content or "") + "\n\n" + text
        elif isinstance(content, list):
            content.append({"type": "text", "text": text})
        else:
            messages.append({"role": "user", "content": text})
    else:
        messages.append({"role": "user", "content": text})
