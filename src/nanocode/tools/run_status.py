"""Schema for reading one subagent run snapshot."""

SCHEMA = {
    "name": "run_status",
    "description": "Read a sub-agent run status snapshot from status.json.",
    "input_schema": {
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string", "description": "Child session id / run id."},
        },
        "required": ["child_session_id"],
    },
}
