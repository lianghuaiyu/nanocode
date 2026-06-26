"""nanocode tool system — schemas, execution, permissions."""

from .registry import (
    ToolDef, ToolRegistry, REGISTRY,
    get_active_tool_definitions, get_deferred_tool_names, reset_activated_tools,
)
from .spec import Tool, coding_tools, read_only_tools
from .types import Capability, ToolSource, Trust
from .permissions import (
    PermissionMode, READ_TOOLS, EDIT_TOOLS, CONCURRENCY_SAFE_TOOLS,
    check_permission, load_permission_rules, reset_permission_cache,
    load_agents_config,
    PermissionEngine, Decision, allowlist_blocks,
)
from .execute import execute_tool
from .run_shell import is_dangerous

__all__ = [
    "ToolDef", "ToolRegistry", "REGISTRY",
    "get_active_tool_definitions", "get_deferred_tool_names", "reset_activated_tools",
    "Tool", "Capability", "ToolSource", "Trust",
    "read_only_tools", "coding_tools",
    "PermissionMode", "READ_TOOLS", "EDIT_TOOLS", "CONCURRENCY_SAFE_TOOLS",
    "check_permission", "load_permission_rules", "reset_permission_cache",
    "load_agents_config",
    "PermissionEngine", "Decision", "allowlist_blocks",
    "execute_tool", "is_dangerous",
]
