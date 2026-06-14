"""Lightweight agent-profile public surface.

The default package import exposes dataclass/profile types only. Discovery and
registry functions are loaded lazily so type users do not need frontmatter/YAML
dependencies.
"""

from __future__ import annotations

from .profile import (
    AGENT_MODES,
    AgentProfile,
    ContextProfile,
    HookPolicy,
    IsolationPolicy,
    McpServerRef,
    MemoryPolicy,
    PermissionProfile,
)

__all__ = [
    "AgentProfile",
    "PermissionProfile",
    "ContextProfile",
    "MemoryPolicy",
    "HookPolicy",
    "IsolationPolicy",
    "McpServerRef",
    "AGENT_MODES",
    "RESERVED_AGENT_TYPES",
    "build_agent_descriptions",
    "build_profile",
    "discover_custom_agents",
    "effective_tools",
    "get_available_agent_types",
    "reset_agent_cache",
]

_REGISTRY_EXPORTS = {
    "RESERVED_AGENT_TYPES",
    "build_agent_descriptions",
    "build_profile",
    "discover_custom_agents",
    "effective_tools",
    "get_available_agent_types",
    "reset_agent_cache",
}


def __getattr__(name: str):
    if name in _REGISTRY_EXPORTS:
        from . import registry as _registry
        return getattr(_registry, name)
    raise AttributeError(name)
