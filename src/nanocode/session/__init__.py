"""nanocode session persistence and session runtime helpers."""

from .store import get_latest_session_id

__all__ = ["AgentSession", "get_latest_session_id"]


def __getattr__(name: str):
    if name == "AgentSession":
        from .agent import AgentSession
        return AgentSession
    raise AttributeError(name)
