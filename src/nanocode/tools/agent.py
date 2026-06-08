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
            "type": {"type": "string", "description": "Agent type. Built-ins: 'explore' (read-only), 'plan' (read-only planning), 'general'/'coder' (full tools). Custom agent types advertised in the system prompt (from .nanocode/agents or .agents/agents) may also be named. Default: general."},
            "resume": {"type": "string", "description": "Resume a previously persisted sub-agent by its id; reloads its history and appends this prompt"},
            "run_in_background": {"type": "boolean", "description": "Run the sub-agent in the background instead of blocking (default: false)"},
            "timeout_ms": {"type": "integer", "description": "Wall-clock timeout in ms for this sub-agent run. If omitted, the agent definition's timeout-ms (if any) is used; otherwise no wall-clock limit (a turn ceiling still bounds it)."},
        },
        "required": ["description", "prompt"],
    },
}
