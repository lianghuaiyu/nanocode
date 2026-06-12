"""nanocode agents 包（docs/15 §10）—— 正式的 AgentProfile + 发现/权限派生。

替换 `subagents/config.py` 的无类型 dict 配置。agent 是「profile = mode/model/tools/permissions/
context 行为」,不只是一段 prompt（Claude Code / OpenCode 都指向此）。

模块：
  profile.py      AgentProfile + 子策略（PermissionProfile/ContextProfile/...）
  registry.py     user/project 发现 + trust gate + extends 收窄 + build_profile（docs/16 #7：
                  subagents/config.py 的配置代数整体在此，dict API 已退役）
  permissions.py  profile 权限派生（child≤parent，Phase 5/6）
  result.py       typed ResultEnvelope（Phase 6）
"""

from .profile import (
    AgentProfile,
    PermissionProfile,
    ContextProfile,
    MemoryPolicy,
    HookPolicy,
    IsolationPolicy,
    McpServerRef,
    AGENT_MODES,
)
from .registry import (
    RESERVED_AGENT_TYPES,
    build_agent_descriptions,
    build_profile,
    discover_custom_agents,
    effective_tools,
    get_available_agent_types,
    reset_agent_cache,
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
