"""tool_search 元工具的 schema。执行（激活 deferred 工具）在 tools.execute 中处理。"""

from __future__ import annotations

SCHEMA = {
    "name": "tool_search",
    "description": "Search for available tools by name or keyword. Returns full schema definitions for matching deferred tools so you can use them.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Tool name or search keywords"},
        },
        "required": ["query"],
    },
}
