"""Lightweight public surface for the agent package.

Importing ``nanocode.agent`` must not import provider SDKs or the runtime/session
layers. The heavy ``Agent`` implementation is imported only when callers
explicitly request it.
"""

from __future__ import annotations

__all__ = ["Agent"]


def __getattr__(name: str):
    if name == "Agent":
        from .engine import Agent
        return Agent
    raise AttributeError(name)
