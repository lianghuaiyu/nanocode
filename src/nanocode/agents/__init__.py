"""nanocode agents 包（docs/15 §10）—— 正式的 AgentProfile + 发现/权限派生。

替换 `subagents/config.py` 的无类型 dict 配置。agent 是「profile = mode/model/tools/permissions/
context 行为」,不只是一段 prompt（Claude Code / OpenCode 都指向此）。

模块：
  profile.py      AgentProfile + 子策略（PermissionProfile/ContextProfile/...）
  registry.py     user/project/plugin/builtin 发现 + extends 解析（Phase 5）
  builtin.py      build/plan/explore/general/system agents（Phase 5）
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

__all__ = [
    "AgentProfile",
    "PermissionProfile",
    "ContextProfile",
    "MemoryPolicy",
    "HookPolicy",
    "IsolationPolicy",
    "McpServerRef",
    "AGENT_MODES",
]
