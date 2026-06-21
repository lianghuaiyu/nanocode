"""Schema for steering a child-session subagent run."""

SCHEMA = {
    "name": "run_send",
    "description": (
        "Queue a steer/follow_up prompt for a child-session sub-agent run. "
        "delivery=steer is injected before the next LLM call; delivery=follow_up "
        "continues when the child would otherwise stop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string", "description": "Child session id / run id."},
            "prompt": {"type": "string", "description": "Steering prompt to append to the child session."},
            "delivery": {
                "type": "string",
                "description": "steer or follow_up. Default: steer.",
            },
            "wake": {
                "type": "boolean",
                "description": "Whether this send is allowed to wake an idle child. Default: false.",
            },
        },
        "required": ["child_session_id", "prompt"],
    },
}
