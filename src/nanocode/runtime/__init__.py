"""nanocode runtime package — thread, child-session, and multi-agent orchestration (L4).

This package keeps its public surface lazy so importing ``nanocode.runtime.spawn``
does not also import the in-process facade and agent session machinery.
"""

_FACADE_EXPORTS = {
    "AgentConfig",
    "AgentResult",
    "AgentRuntime",
    "ApprovalManager",
    "ApprovalRequest",
    "ReadOnlySessionView",
    "RuntimeApprovalBroker",
    "RuntimeServices",
    "RuntimeThread",
    "SkillInvocation",
    "TurnResult",
    "_apply_runtime_services",
    "_push_cwd",
    "serialize_event_envelope",
}

__all__ = sorted(_FACADE_EXPORTS)


def __getattr__(name: str):
    if name in _FACADE_EXPORTS:
        from . import facade as _facade
        return getattr(_facade, name)
    raise AttributeError(name)
