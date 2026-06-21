"""Schema for listing child-session subagent runs."""

SCHEMA = {
    "name": "run_list",
    "description": "List sub-agent child-session runs for the current parent session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional run status filter."},
        },
        "required": [],
    },
}
