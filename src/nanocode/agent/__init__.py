"""Lightweight public surface for the agent package.

Importing ``nanocode.agent`` must not import provider SDKs. Runtime/session types
are loaded lazily, and the heavy ``Agent`` implementation is imported only when
callers explicitly request it.
"""

from __future__ import annotations

__all__ = [
    "Agent",
    "AgentSession",
    "AgentRuntime",
    "RuntimeThread",
    "TurnResult",
    "AgentResult",
    "SkillInvocation",
    "ApprovalManager",
    "ApprovalRequest",
    "RuntimeApprovalBroker",
    "AgentConfig",
]


def __getattr__(name: str):
    if name == "Agent":
        from .engine import Agent
        return Agent
    if name == "AgentSession":
        from .session import AgentSession
        return AgentSession
    if name in {
        "AgentRuntime",
        "RuntimeThread",
        "TurnResult",
        "AgentResult",
        "SkillInvocation",
        "ApprovalManager",
        "ApprovalRequest",
        "RuntimeApprovalBroker",
        "AgentConfig",
    }:
        from . import runtime as _runtime
        return getattr(_runtime, name)
    raise AttributeError(name)
