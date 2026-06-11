"""nanocode capabilities 包（docs/15 §5）—— tools/MCP/skills/subagents 的单一 dispatch + 权限。

模块：
  permissions.py  PermissionContext（不可变权限上下文,取代 PermissionEngine 对 live Agent 的 back-ref）
                  + decide()。
  router.py       CapabilityRouter dispatch taxonomy（meta/agent/skill/plan/real 分类 + 单一 allowlist
                  咽喉点判定）。

迁移策略：先落地 **PermissionContext + 分类 taxonomy**（additive,可测）;engine._execute_tool_call
改为经 router 派发（消除 tools↔agent 循环 import、保单一 allowlist 咽喉点）是后续 cutover 步骤。
"""

from .permissions import PermissionContext, decide
from .router import (
    Capability,
    classify_capability,
    is_always_allowed_meta,
    router_allowlist_blocks,
)

__all__ = [
    "PermissionContext",
    "decide",
    "Capability",
    "classify_capability",
    "is_always_allowed_meta",
    "router_allowlist_blocks",
]
