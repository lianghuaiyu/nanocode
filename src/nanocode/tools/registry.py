"""工具注册表：tool schema 聚合（从 spec.TOOLS 派生）+ deferred 工具的激活/查询。

docs/16 #5：本模块不再手维护 schema 列表——`spec.TOOLS` 是单一真相源，此处只保留
deferred 激活状态与查询门面（消除 registry/execute 两份注册表漂移）。
"""

from __future__ import annotations

from .spec import TOOLS, ToolSpec  # noqa: F401 — re-export 单一真相源

ToolDef = dict  # Anthropic tool schema dict

tool_definitions: list[ToolDef] = [s.schema for s in TOOLS.values()]

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
