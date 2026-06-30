"""nanocode extensions — Pi-aligned system extension host (docs/22 §5.0 / §7).

Importing this package has NO side effects: it does not discover project/user
extensions, does not activate anything, and does not start background workers
(docs/22 §9.1.3). Build a host explicitly via
`ExtensionHost.load_system_extensions().activate_all()`.
"""
from .errors import ExtensionError, ExtensionLoadError, ExtensionRuntimeError
from .host import ExtensionHost
from .manifest import (
    CommandContribution, ExtensionContributes, ExtensionManifest, McpServerSpec,
)

__all__ = [
    "ExtensionHost",
    "ExtensionManifest",
    "ExtensionContributes",
    "CommandContribution",
    "McpServerSpec",
    "ExtensionError",
    "ExtensionLoadError",
    "ExtensionRuntimeError",
]
