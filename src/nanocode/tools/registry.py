"""工具注册表：聚合所有工具 schema，并提供 deferred 工具的激活/查询。"""

from __future__ import annotations

ToolDef = dict  # Anthropic tool schema dict

from . import (
    read_file, write_file, edit_file, list_files, grep_search,
    run_shell, sandbox_shell, web_fetch, skill, agent, plan, tool_search,
    tasks_tool, memory_tool,
)

tool_definitions: list[ToolDef] = [
    read_file.SCHEMA, write_file.SCHEMA, edit_file.SCHEMA,
    list_files.SCHEMA, grep_search.SCHEMA, run_shell.SCHEMA, sandbox_shell.SCHEMA,
    skill.SCHEMA, web_fetch.SCHEMA, *plan.SCHEMAS,
    agent.SCHEMA, tool_search.SCHEMA,
    tasks_tool.LIST_SCHEMA, tasks_tool.OUTPUT_SCHEMA, tasks_tool.STOP_SCHEMA,
    memory_tool.SCHEMA,
]

_activated_tools: set[str] = set()


def reset_activated_tools() -> None:
    _activated_tools.clear()


def get_active_tool_definitions(all_tools: list[ToolDef] | None = None) -> list[ToolDef]:
    """Return tool definitions, excluding deferred tools that haven't been activated.
    Strips the 'deferred' key so it's not sent to the API."""
    tools = all_tools if all_tools is not None else tool_definitions
    return [
        {k: v for k, v in t.items() if k != "deferred"}
        for t in tools
        if not t.get("deferred") or t["name"] in _activated_tools
    ]


def get_deferred_tool_names(all_tools: list[ToolDef] | None = None) -> list[str]:
    """Return names of deferred tools that haven't been activated yet."""
    tools = all_tools if all_tools is not None else tool_definitions
    return [t["name"] for t in tools if t.get("deferred") and t["name"] not in _activated_tools]
