"""skill 元工具的 schema。执行逻辑在 agent.engine 中处理（避免循环依赖）。"""

from __future__ import annotations

SCHEMA = {
    "name": "skill",
    "description": "Invoke a registered skill by name. Skills are prompt templates loaded from .nanocode/skills/. Returns the skill's resolved prompt to follow.",
    "input_schema": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "The name of the skill to invoke"},
            "args": {"type": "string", "description": "Optional arguments to pass to the skill"},
        },
        "required": ["skill_name"],
    },
}
