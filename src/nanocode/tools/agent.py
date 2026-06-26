"""agent 元工具的 schema。执行逻辑在 runtime/spawn.py 中处理（避免循环依赖）。"""

from __future__ import annotations

_STEP_PROPS = {
    "type": {"type": "string", "description": "Agent type for this step (same vocabulary as the top-level 'type')."},
    "description": {"type": "string", "description": "Short description of this step."},
    "prompt": {"type": "string", "description": "Task instructions for this step."},
    "timeout_ms": {"type": "integer", "description": "Optional per-step wall-clock timeout in ms."},
    "context": {"type": "object", "description": "Explicit context mode. Default: {'mode':'fresh'}."},
    "isolation": {"type": "string", "description": "shared or worktree."},
}

SCHEMA = {
    "name": "agent",
    "description": (
        "Launch a sub-agent to handle a task autonomously. Sub-agents have isolated context and "
        "return their result. Types: 'explore' (read-only), 'plan' (read-only, structured planning), "
        "'general'/'coder' (full tools). Orchestration: pass 'steps' to run a CHAIN of sub-agents "
        "sequentially (each step's prompt may contain the literal {previous} placeholder, replaced "
        "with the previous step's result envelope), or 'tasks' to fan out independent sub-agents in "
        "PARALLEL and gather their bounded results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short (3-5 word) description of the sub-agent's task"},
            "prompt": {"type": "string", "description": "Detailed task instructions for the sub-agent (single-run mode; omit when using steps/tasks)"},
            "type": {"type": "string", "description": "Agent type. Built-ins: 'explore' (read-only), 'plan' (read-only planning), 'general'/'coder' (full tools). Custom agent types advertised in the system prompt (from .nanocode/agents or .agents/agents) may also be named. Default: general."},
            "context": {"type": "object", "description": "Explicit context mode. Default: {'mode':'fresh'}; supported: fresh, fork_summary, branch_projection."},
            "isolation": {"type": "string", "description": "Execution isolation: shared or worktree."},
            "resume": {"type": "string", "description": "Resume a child-session sub-agent by child_session_id and append this prompt"},
            "steer": {"type": "string", "description": "Queue a steering prompt for a running child session id without creating a new child."},
            "delivery": {"type": "string", "description": "For steer: steer or follow_up. Default: steer."},
            "wake": {"type": "boolean", "description": "For resume/steer: whether to wake an idle child. resume defaults true; steer defaults false."},
            "run_in_background": {"type": "boolean", "description": "Run the sub-agent in the background instead of blocking (default: false). The host reports completion later; do not sleep, poll, or proactively check progress."},
            "timeout_ms": {"type": "integer", "description": "Wall-clock timeout in ms for this sub-agent run. If omitted, the agent definition's timeout-ms (if any) is used; otherwise no wall-clock limit (a turn ceiling still bounds it)."},
            "steps": {
                "type": "array",
                "description": "CHAIN mode: run these steps sequentially, each as an independent sub-agent. {previous} in a step prompt is replaced with the previous step's result. Mutually exclusive with prompt/tasks/resume/run_in_background.",
                "items": {"type": "object", "properties": _STEP_PROPS, "required": ["prompt"]},
            },
            "tasks": {
                "type": "array",
                "description": "PARALLEL mode: fan out these independent sub-agents concurrently and gather their bounded results. Mutually exclusive with prompt/steps/resume/run_in_background.",
                "items": {"type": "object", "properties": _STEP_PROPS, "required": ["prompt"]},
            },
        },
        "required": [],
    },
}


async def run(ctx, inp: dict) -> str:
    """host-routed：纯转发 ctx.spawn.agent（编排逻辑留在 runtime/spawn.py、engine）。"""
    return await ctx.spawn.agent(inp)
