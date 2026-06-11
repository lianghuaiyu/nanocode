"""capabilities/router.py — CapabilityRouter dispatch taxonomy（docs/15 §5/§11）。

把 engine._execute_tool_call 的派发分类提成纯函数 taxonomy：一个工具调用属于哪一类
（always-allowed 宿主 meta / agent spawn / skill / plan-mode / 真实工具）+ 单一 allowlist 咽喉点判定。
这是「单一 dispatch」router 的可复用核心 —— engine 改为经此分类派发是后续 cutover（消除
tools↔agent 循环 import、保 allowlist fail-closed 不被任何新路径绕过）。

安全不变量（§5 风险 #3）：allowlist 判定必须在任何真实工具派发之前 fail-closed；'agent' 对子 agent
一律拦截（独立后备）。这里复用 tools.permissions.allowlist_blocks（单一真相,不重实现）。
"""

from __future__ import annotations

from enum import Enum

from ..tools.permissions import (
    ALWAYS_ALLOWED_META, AGENT_META_TOOL, allowlist_blocks,
)


class Capability(str, Enum):
    """一次工具调用的派发类别。"""

    META = "meta"          # 纯宿主 meta（task_*/plan_mode）：不经 execute_tool、无持久副作用
    AGENT = "agent"        # spawn 子 agent（子不可调用）
    SKILL = "skill"        # skill 调用（fork/hook 可触 shell,受 allowlist 约束）
    MEMORY = "memory"      # memory 工具（save/update/delete 落真实 memory_tool,受约束）
    REAL = "real"          # 真实工具（mcp / execute_tool）


# plan-mode 切换工具（纯宿主状态切换,主 agent 专用）。
_PLAN_MODE_TOOLS = frozenset({"enter_plan_mode", "exit_plan_mode"})
# 后台任务面板 meta（只读,无持久副作用）。
_TASK_META_TOOLS = frozenset({"task_list", "task_output", "task_stop"})


def classify_capability(name: str) -> Capability:
    """工具名 → 派发类别（与 engine._execute_tool_call 的分支顺序语义一致）。"""
    if name in _PLAN_MODE_TOOLS or name in _TASK_META_TOOLS:
        return Capability.META
    if name == AGENT_META_TOOL:
        return Capability.AGENT
    if name == "skill":
        return Capability.SKILL
    if name == "memory":
        return Capability.MEMORY
    return Capability.REAL


def is_always_allowed_meta(name: str) -> bool:
    """该工具是否属 call-time allowlist 永不约束的纯宿主 meta（task_*/plan_mode）。"""
    return name in ALWAYS_ALLOWED_META


def router_allowlist_blocks(name: str, allowed_tool_names: "set[str] | frozenset[str] | None") -> bool:
    """单一 allowlist 咽喉点（复用 tools.permissions.allowlist_blocks,不重实现）。

    - allowed_tool_names 为 None（主 agent）→ 永不拦截。
    - 'agent' → 子 agent 一律拦截（独立 fail-closed 后备）。
    - 纯宿主 meta（task_*/plan_mode）→ 放行。
    - 其余（含 memory/skill/run_shell/真实工具/MCP）→ 不在有效集内即拦截。
    """
    return allowlist_blocks(name, allowed_tool_names)
