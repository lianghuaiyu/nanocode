"""agent 元工具的 schema。执行逻辑在 agent.engine 中处理（避免循环依赖）。"""

from __future__ import annotations

SCHEMA = {
    "name": "agent",
    "description": "Launch a sub-agent to handle a task autonomously. Sub-agents have isolated context and return their result. Types: 'explore' (read-only), 'plan' (read-only, structured planning), 'general'/'coder' (full tools).",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short (3-5 word) description of the sub-agent's task"},
            "prompt": {"type": "string", "description": "Detailed task instructions for the sub-agent"},
            "type": {"type": "string", "enum": ["explore", "plan", "general", "coder"], "description": "Agent type. Default: general"},
            "resume": {"type": "string", "description": "Resume a previously persisted sub-agent by its id; reloads its history and appends this prompt"},
            "run_in_background": {"type": "boolean", "description": "Run the sub-agent in the background instead of blocking (default: false)"},
        },
        "required": ["description", "prompt"],
    },
}
