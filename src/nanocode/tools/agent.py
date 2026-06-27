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

# acceptance-gate（docs/26 §0.6 策略库）:worker 生产 → 验证(reviewer agent 和/或 output_schema)
# → 不过则带反馈 retry，至多 max_rounds。worker/reviewer 子 spec 用 _STEP_PROPS 词汇。
_ACCEPT_PROPS = {
    "worker": {"type": "object", "properties": _STEP_PROPS,
               "description": "Required. The agent that produces the output to be verified."},
    "reviewer": {"type": "object", "properties": _STEP_PROPS,
                 "description": "Optional LLM verifier. Its prompt should contain the literal {output} "
                                "(replaced with the worker's raw output) and it MUST emit JSON "
                                "{\"accept\": bool, \"feedback\": str}."},
    "output_schema": {"type": "object",
                      "description": "Optional deterministic verifier: a lightweight structural schema "
                                     "(type/required/properties/items subset, not full JSON Schema) the "
                                     "worker output (parsed as JSON) must satisfy; violations become "
                                     "feedback for the next round."},
    "max_rounds": {"type": "integer", "description": "Max produce→verify→retry rounds (default 3, capped at 5)."},
}

# plan-then-fanout（docs/26 §0.6 策略库）:planner 输出 JSON 子任务列表 → 并发 fan out workers。
_PLAN_FANOUT_PROPS = {
    "planner": {"type": "object", "properties": _STEP_PROPS,
                "description": "Required. Emits a JSON array of {description, prompt, type?} subtasks "
                               "(or {\"subtasks\": [...]})."},
    "worker_type": {"type": "string", "description": "Default agent type for subtasks that omit 'type' (default: coder)."},
    "max_workers": {"type": "integer", "description": "Cap on fanned-out workers (default/cap 8; extra subtasks dropped with a notice)."},
}

SCHEMA = {
    "name": "agent",
    "description": (
        "Launch a sub-agent to handle a task autonomously. Sub-agents have isolated context and "
        "return their result. Types: 'explore' (read-only), 'plan' (read-only, structured planning), "
        "'general'/'coder' (full tools). Orchestration: pass 'steps' to run a CHAIN of sub-agents "
        "sequentially (each step's prompt may contain the literal {previous} placeholder, replaced "
        "with the previous step's result envelope), or 'tasks' to fan out independent sub-agents in "
        "PARALLEL and gather their bounded results. Add run_in_background to either to run the whole "
        "orchestration detached: it returns immediately with a group id, each member's summary is "
        "auto-injected on completion, and 'run_cancel <group_id>' cancels the entire group. "
        "Verified work: pass 'accept' to run a produce→verify→retry loop (a reviewer sub-agent and/or "
        "an output_schema check gate the worker's output, with feedback-driven retries). Dynamic "
        "decomposition: pass 'plan_fanout' to have a planner sub-agent emit subtasks that are then "
        "fanned out as parallel workers. 'steps'/'tasks'/'accept'/'plan_fanout' are mutually exclusive; "
        "'accept' and 'plan_fanout' run foreground only."
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
            "run_in_background": {"type": "boolean", "description": "Run the sub-agent in the background instead of blocking (default: false). The host reports completion later; do not sleep, poll, or proactively check progress. May be combined with steps/tasks to run the whole chain/parallel orchestration detached (returns a group id)."},
            "timeout_ms": {"type": "integer", "description": "Wall-clock timeout in ms for this sub-agent run. If omitted, the agent definition's timeout-ms (if any) is used; otherwise no wall-clock limit (a turn ceiling still bounds it)."},
            "steps": {
                "type": "array",
                "description": "CHAIN mode: run these steps sequentially, each as an independent sub-agent. {previous} in a step prompt is replaced with the previous step's result. Mutually exclusive with prompt/tasks/resume; with run_in_background it runs detached and returns a group id (run_cancel <group_id> cancels the chain).",
                "items": {"type": "object", "properties": _STEP_PROPS, "required": ["prompt"]},
            },
            "tasks": {
                "type": "array",
                "description": "PARALLEL mode: fan out these independent sub-agents concurrently and gather their bounded results. Mutually exclusive with prompt/steps/resume; with run_in_background it runs detached and returns a group id (run_cancel <group_id> cancels the whole group).",
                "items": {"type": "object", "properties": _STEP_PROPS, "required": ["prompt"]},
            },
            "accept": {
                "type": "object",
                "description": "ACCEPTANCE-GATE mode: run the worker, verify its output (reviewer sub-agent and/or output_schema), and retry with feedback up to max_rounds until accepted. Requires at least one of reviewer/output_schema. Foreground only; mutually exclusive with the other shapes.",
                "properties": _ACCEPT_PROPS,
                "required": ["worker"],
            },
            "plan_fanout": {
                "type": "object",
                "description": "PLAN-THEN-FANOUT mode: a planner sub-agent emits a JSON array of subtasks, which are then fanned out as parallel workers and aggregated. Foreground only; mutually exclusive with the other shapes.",
                "properties": _PLAN_FANOUT_PROPS,
                "required": ["planner"],
            },
        },
        "required": [],
    },
}


async def run(ctx, inp: dict) -> str:
    """host-routed：纯转发 ctx.spawn.agent（编排逻辑留在 runtime/spawn.py、engine）。"""
    return await ctx.spawn.agent(inp)
