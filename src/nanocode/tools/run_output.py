"""Schema for reading subagent run output."""

SCHEMA = {
    "name": "run_output",
    "description": (
        "Read sub-agent output from its durable run record when the user asks or after a completion notice. "
        "Do not repeatedly poll running background runs."
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
