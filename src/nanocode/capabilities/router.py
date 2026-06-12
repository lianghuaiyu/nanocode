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
from .host import ToolHost


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


class CapabilityRouter:
    """工具派发的单一入口（docs/15 §5）：allowlist fail-closed 咽喉点 + meta/agent/skill/real 路由 + hooks。

    host-driven（host: ToolHost——typed port,docs/16 #5；Agent 结构性满足）。从 engine._execute_tool_call 逐字搬迁,行为不变。
    'agent'/'skill' 经 host 方法路由（**不 import engine**）——结构上消除 execute.py 注释提到的
    tools↔agent 循环 import。allowlist 判定（host._tool_blocked_by_allowlist）仍是第一道、覆盖前台 +
    后台 run_shell 的单一 fail-closed 咽喉点（§5 风险#3）。
    """

    async def dispatch(self, host: ToolHost, name: str, inp: dict) -> str:
        # P4 call-time allowlist enforcement（安全基石）：任何真实工具派发（含 run_shell 后台分支）之前 fail-closed。
        if host._tool_blocked_by_allowlist(name):
            from ..agent.events import ToolBlocked   # lazy：避免 capabilities↔agent 包级 import 环
            host.emit(ToolBlocked(tool=name, reason="not_in_allowlist"))
            return f"Error: tool '{name}' is not permitted for this sub-agent."
        if name == "run_shell" and inp.get("run_in_background"):
            tid = await host._spawn_background_shell(inp.get("command", ""), inp.get("timeout"))
            return (f"Started background shell task {tid}. It will report completion later. "
                    f"Use task_output with task_id={tid} to inspect progress.")
        from ..tools import tasks_tool
        if name == "task_list":
            return tasks_tool.list_tasks_text(host.task_manager, inp.get("status"), inp.get("kind"))
        if name == "task_output":
            return tasks_tool.task_output_text(host.task_manager, inp.get("task_id", ""),
                                               int(inp.get("tail_bytes") or 8000))
        if name == "task_stop":
            return await tasks_tool.task_stop(
                host.task_manager, host._background_tasks, inp.get("task_id", ""),
                allow_orphan_cancel=not host.is_sub_agent)
        if name == "memory" and inp.get("action") == "recall" and inp.get("semantic"):
            return await host._recall_memory_semantic(inp.get("query", ""), int(inp.get("limit") or 5))
        if name == "memory" and inp.get("action") == "consolidate":
            if host.is_sub_agent:
                return ("Error: memory consolidation is a host/session operation and "
                        "is not available to sub-agents.")
            return await host._spawn_memory_consolidate()
        if name in ("enter_plan_mode", "exit_plan_mode"):
            if host.is_sub_agent:
                return "Error: plan-mode tools are not available to sub-agents."
            return await host._execute_plan_mode_tool(name)
        if name == "agent":
            return await host._execute_agent_tool(inp)
        if name == "skill":
            return await host._execute_skill_tool(inp)
        # 真实工具(mcp/execute_tool)——受 hooks 约束(meta 工具上面已返回)。
        if name in ("run_shell", "sandbox_shell") and "_session_id" not in inp:
            inp = {**inp, "_session_id": host.session_id}
        if host._suppress_hooks or not host._active_hooks:
            return await host._run_real_tool(name, inp)
        for h in host._matching_hooks("pre-tool-use", name):
            ok, msg = await host._run_hook(h, name, inp, None)
            if not ok:
                return f"[blocked by skill hook {h['skill']} (pre-tool-use)] {msg}"
        result = await host._run_real_tool(name, inp)
        warnings = []
        for h in host._matching_hooks("post-tool-use", name):
            ok, msg = await host._run_hook(h, name, inp, result)
            if not ok:
                warnings.append(f"[skill hook {h['skill']} (post-tool-use) warning] {msg}")
        if warnings:
            result = result + "\n\n" + "\n".join(warnings)
        return result
