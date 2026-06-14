"""Compatibility re-export for AgentSession.

The implementation lives in :mod:`nanocode.session.agent` because it owns the
state <-> canonical session tree boundary. Keep this module so older imports
such as ``nanocode.agent.session.AgentSession`` continue to work while session
runtime code moves out of ``agent/``.
"""

from ..session.agent import AgentSession

__all__ = ["AgentSession"]
