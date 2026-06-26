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


async def run_enter(ctx, inp: dict) -> str:
    """host-routed：进入 plan 模式（主 agent 专用；ctx.set_mode 子 agent 为 None + router 守卫）。"""
    return await ctx.set_mode.enter_plan()


async def run_exit(ctx, inp: dict) -> str:
    """host-routed：退出 plan 模式（含交互审批）。"""
    return await ctx.set_mode.exit_plan()


# 按 schema name 选 run（spec._ALL 构造期绑定）。
RUNS = {"enter_plan_mode": run_enter, "exit_plan_mode": run_exit}
