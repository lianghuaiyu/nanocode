"""Schema for reading one subagent run snapshot."""

SCHEMA = {
    "name": "run_status",
    "description": (
        "Read one sub-agent run status snapshot through the runtime ledger when the user explicitly asks. "
        "Do not repeatedly poll background runs; the host reports completion."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string", "description": "Child session id / run id."},
        },
        "required": ["child_session_id"],
    },
}


def run(ctx, inp: dict) -> str:
    return ctx.runs.status(inp.get("child_session_id", ""))
