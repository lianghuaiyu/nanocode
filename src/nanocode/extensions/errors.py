"""extensions/errors.py — extension host error types (docs/22 §7 Phase 0)."""
from __future__ import annotations


class ExtensionError(Exception):
    """Base for extension host errors."""


class ExtensionLoadError(ExtensionError):
    """Raised when a manifest cannot be loaded/activated, or a registration
    conflict is detected at startup (fail loud — never silently drop)."""


class ExtensionRuntimeError(ExtensionError):
    """Raised when a host action is used before bind, or on a stale (invalidated)
    extension context after a session replacement/teardown."""
