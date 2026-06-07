"""enter/exit_plan_mode 元工具的 schema（均为 deferred）。
执行逻辑在 agent.engine 中处理（避免循环依赖）。"""

from __future__ import annotations

SCHEMAS = [
    {
        "name": "enter_plan_mode",
        "description": "Enter plan mode to switch to a read-only planning phase. In plan mode, you can only read files and write to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
        "deferred": True,
    },
    {
        "name": "exit_plan_mode",
        "description": "Exit plan mode after you have finished writing your plan to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
        "deferred": True,
    },
]
