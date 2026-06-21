"""Schema for structured subagent result lookup."""

SCHEMA = {
    "name": "get_subagent_result",
    "description": (
        "Read a sub-agent run result by child_session_id from its durable run record when the user asks "
        "or after the host reports completion. This does not parse task_output text or read the child "
        "transcript. Do not repeatedly poll running background runs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string", "description": "Child session id / run id."},
            "include_events": {"type": "boolean", "description": "Include events.jsonl tail."},
            "tail_events": {"type": "integer", "description": "Number of recent events to include."},
        },
        "required": ["child_session_id"],
    },
}
