"""tools/spec.py — ToolSpec：schema + executor + 元数据的单一真相源（docs/16 #5）。

收掉两份按名漂移的注册表：`registry.tool_definitions` 与 `execute._HANDLERS` 均从 `TOOLS` 派生。
host-routed 工具（agent/skill/plan/task_*/memory-host 分支/tool_search）`run=None`——它们经
CapabilityRouter 分发到 host 方法或 execute_tool 的专用分支，不走通用 handler。

安全边界（docs/16 §2 不变量）：allowlist + PermissionEngine 检查**留在 dispatch 咽喉点**
（capabilities/router.py + engine._authorize_dispatch），绝不下推进工具函数；`concurrency_safe`
分类的真相源仍是 permissions.CONCURRENCY_SAFE_TOOLS（安全相邻分类归权限层）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import (
    read_file, write_file, edit_file, list_files, grep_search,
    run_shell, sandbox_shell, web_fetch, skill, agent, plan, tool_search,
    tasks_tool, memory_tool,
)
from .permissions import CONCURRENCY_SAFE_TOOLS


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整规格：schema（API 形状）+ run（纯函数 executor，host-routed 为 None）+ 元数据。"""

    schema: dict
    run: "Callable[[dict], str] | None" = None
    concurrency_safe: bool = False
    deferred: bool = False

    @property
    def name(self) -> str:
        return self.schema["name"]


def _spec(schema: dict, run: "Callable[[dict], str] | None" = None) -> ToolSpec:
    return ToolSpec(schema=schema, run=run,
                    concurrency_safe=schema["name"] in CONCURRENCY_SAFE_TOOLS,
                    deferred=bool(schema.get("deferred")))


# 顺序 = 发给 API 的工具顺序（沿袭原 registry.tool_definitions 字面顺序，勿乱动）。
_ALL: list[ToolSpec] = [
    _spec(read_file.SCHEMA, read_file.run),
    _spec(write_file.SCHEMA, write_file.run),
    _spec(edit_file.SCHEMA, edit_file.run),
    _spec(list_files.SCHEMA, list_files.run),
    _spec(grep_search.SCHEMA, grep_search.run),
    _spec(run_shell.SCHEMA, run_shell.run),        # 前台经 execute._route_run_shell（plan_shell 单一规划器）
    _spec(sandbox_shell.SCHEMA, sandbox_shell.run),
    _spec(skill.SCHEMA),                            # host-routed：CapabilityRouter → _execute_skill_tool
    _spec(web_fetch.SCHEMA, web_fetch.run),
    *[_spec(s) for s in plan.SCHEMAS],              # host-routed：plan-mode 状态切换（主 agent 专用）
    _spec(agent.SCHEMA),                            # host-routed：spawn 子 agent
    _spec(tool_search.SCHEMA),                      # execute.py 专用分支：激活 deferred 工具
    _spec(tasks_tool.LIST_SCHEMA),                  # host-routed：task 面板 meta（router 分发）
    _spec(tasks_tool.OUTPUT_SCHEMA),
    _spec(tasks_tool.STOP_SCHEMA),
    _spec(memory_tool.SCHEMA, memory_tool.run),     # recall-semantic/consolidate 分支由 router 先截
]

TOOLS: dict[str, ToolSpec] = {s.name: s for s in _ALL}
if len(TOOLS) != len(_ALL):                          # 重名即 bug，构造期 fail-loud
    raise RuntimeError("duplicate tool name in TOOLS registry")


# ─── curated bundles（pi packages/agent index.ts:138-154 同位；供 subagent 配置引用,docs/16 #9）──

def read_only_tools() -> list[str]:
    """只读 bundle：浏览/检索，无写入、无 shell、无 spawn。"""
    return ["read_file", "list_files", "grep_search", "web_fetch", "tool_search"]


def coding_tools() -> list[str]:
    """编码 bundle：只读 + 写入/编辑 + 前台 shell（仍受 PermissionEngine/sandbox 约束）。"""
    return read_only_tools() + ["write_file", "edit_file", "run_shell"]
