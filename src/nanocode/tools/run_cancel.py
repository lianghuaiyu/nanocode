"""Schema for cancelling a subagent run."""

SCHEMA = {
    "name": "run_cancel",
    "description": "Cancel a live sub-agent run by child session id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string", "description": "Child session id / run id."},
        },
        "required": ["child_session_id"],
    },
}
