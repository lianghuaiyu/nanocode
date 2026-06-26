"""tools/spec.py — Tool：schema + executor + 能力元数据的单一真相源（docs/16 #5 / docs/24 Phase 1）。

工具的完整规格合一为一个 `Tool` 数据类：schema（模型可见入参，闭合 additionalProperties=false）+
run（纯函数 executor，host-routed 为 None）+ 分类元数据（concurrency_safe/deferred）+ 能力声明
（needs/source/trust）。`_ALL` 字面顺序 = 发给 API 的工具顺序（load-bearing）；`ToolRegistry`
（registry.py）从 `_ALL` 构造单例 REGISTRY，是工具真相源的唯一门面。

host-routed 工具（agent/skill/plan/task_*/run_*/memory/run_shell/tool_search）`run=None`——它们经
CapabilityRouter 分发到 host 方法或 execute_tool 的专用分支，不走通用 handler。

能力声明（docs/24 §4.1）：Phase 1 **仅声明不强制**——`needs`/`source`/`trust` 是惰性元数据，
dispatch 尚未消费（Phase 2 起按「声明 ∩ 信任档策略」铸造能力把手）。内置工具保守声明 needs。

安全边界（docs/16 §2 不变量）：allowlist + PermissionEngine 检查**留在 dispatch 咽喉点**
（capabilities/router.py + engine._authorize_dispatch），绝不下推进工具函数；`concurrency_safe`
分类的真相源仍是 permissions.CONCURRENCY_SAFE_TOOLS（安全相邻分类归权限层）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from . import (
    read_file, write_file, edit_file, list_files, grep_search,
    run_shell, web_fetch, skill, agent, plan, tool_search,
    tasks_tool, memory_tool, get_subagent_result, run_list, run_status,
    run_output, run_cancel, run_send,
)
from .permissions import CONCURRENCY_SAFE_TOOLS
from .types import Capability, ToolSource, Trust


@dataclass(frozen=True)
class Tool:
    """一个工具的完整规格：schema（API 形状）+ run（纯函数 executor，host-routed 为 None）+ 元数据。

    能力字段（needs/source/trust）为 docs/24 Phase 1 惰性声明：仅记录，dispatch 尚未消费。
    """

    schema: dict
    run: "Callable[[ToolContext, dict], str] | None" = None
    concurrency_safe: bool = False
    deferred: bool = False
    needs: frozenset[Capability] = field(default_factory=frozenset)
    source: ToolSource = ToolSource.BUILTIN
    trust: Trust = Trust.BUILTIN

    @property
    def name(self) -> str:
        return self.schema["name"]


def _closed(schema: dict) -> dict:
    """schema 的闭合副本：`input_schema.additionalProperties = false`（docs/19 §4.2 / Phase 1）。

    单一真相源在此统一收口——所有 public tool schema 对模型与 validator 都是闭合的，未知键
    被拒、不 silent strip。深拷贝避免改动各工具模块的 SCHEMA 字面量。
    """
    out = copy.deepcopy(schema)
    insch = out.setdefault("input_schema", {"type": "object", "properties": {}})
    insch["additionalProperties"] = False
    return out


def _spec(
    schema: dict,
    run: "Callable[[ToolContext, dict], str] | None" = None,
    *,
    needs: frozenset[Capability] = frozenset(),
) -> Tool:
    closed = _closed(schema)
    return Tool(
        schema=closed,
        run=run,
        concurrency_safe=closed["name"] in CONCURRENCY_SAFE_TOOLS,
        deferred=bool(closed.get("deferred")),
        needs=needs,
        source=ToolSource.BUILTIN,
        trust=Trust.BUILTIN,
    )


# 顺序 = 发给 API 的工具顺序（沿袭原 registry.tool_definitions 字面顺序，勿乱动）。
# needs：docs/24 §4 保守声明（Phase 1 仅声明，不强制）。
_ALL: list[Tool] = [
    _spec(read_file.SCHEMA, read_file.run, needs=frozenset({Capability.FS_READ})),
    _spec(write_file.SCHEMA, write_file.run, needs=frozenset({Capability.FS_WRITE})),
    _spec(edit_file.SCHEMA, edit_file.run, needs=frozenset({Capability.FS_WRITE})),
    _spec(list_files.SCHEMA, list_files.run, needs=frozenset({Capability.FS_READ})),
    _spec(grep_search.SCHEMA, grep_search.run, needs=frozenset({Capability.FS_READ})),
    _spec(run_shell.SCHEMA, run_shell.run, needs=frozenset({Capability.EXEC, Capability.TASKS})),   # host-routed：前台经 ctx.exec(SandboxManager)，后台经 ctx.tasks.spawn_shell
    _spec(skill.SCHEMA, skill.run, needs=frozenset({Capability.SPAWN})),      # host-routed：ctx.spawn.skill
    _spec(web_fetch.SCHEMA, web_fetch.run, needs=frozenset({Capability.FS_READ})),
    *[_spec(s, plan.RUNS[s["name"]], needs=frozenset({Capability.SET_MODE})) for s in plan.SCHEMAS],  # host-routed：plan-mode 状态切换（主 agent 专用，ctx.set_mode）
    _spec(agent.SCHEMA, agent.run, needs=frozenset({Capability.SPAWN})),      # host-routed：ctx.spawn.agent
    _spec(tool_search.SCHEMA),                                     # execute.py 专用分支：激活 deferred 工具（无能力需求）
    _spec(tasks_tool.LIST_SCHEMA, tasks_tool.run_list, needs=frozenset({Capability.TASKS})),    # host-routed：ctx.tasks
    _spec(tasks_tool.OUTPUT_SCHEMA, tasks_tool.run_output, needs=frozenset({Capability.TASKS})),
    _spec(tasks_tool.STOP_SCHEMA, tasks_tool.run_stop, needs=frozenset({Capability.TASKS})),
    _spec(get_subagent_result.SCHEMA, get_subagent_result.run, needs=frozenset({Capability.SESSION_READ})),  # host-routed：ctx.runs（run_output 别名）
    _spec(run_list.SCHEMA, run_list.run, needs=frozenset({Capability.SESSION_READ})),
    _spec(run_status.SCHEMA, run_status.run, needs=frozenset({Capability.SESSION_READ})),
    _spec(run_output.SCHEMA, run_output.run, needs=frozenset({Capability.SESSION_READ})),
    _spec(run_cancel.SCHEMA, run_cancel.run, needs=frozenset({Capability.SESSION_READ})),
    _spec(run_send.SCHEMA, run_send.run, needs=frozenset({Capability.SESSION_READ})),
    _spec(memory_tool.SCHEMA, memory_tool.run, needs=frozenset({Capability.MEMORY})),       # host-routed：ctx.memory
]


# ─── curated bundles（pi packages/agent index.ts:138-154 同位；供 subagent 配置引用,docs/16 #9）──

def read_only_tools() -> list[str]:
    """只读 bundle：浏览/检索，无写入、无 shell、无 spawn。"""
    return ["read_file", "list_files", "grep_search", "web_fetch", "tool_search"]


def coding_tools() -> list[str]:
    """编码 bundle：只读 + 写入/编辑 + 前台 shell（仍受 PermissionEngine/sandbox 约束）。"""
    return read_only_tools() + ["write_file", "edit_file", "run_shell"]
