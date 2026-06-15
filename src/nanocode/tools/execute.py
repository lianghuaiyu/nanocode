"""工具调用分发：read-before-edit + mtime 校验、tool_search 激活、结果截断。

shell 执行**不在此**——run_shell 由 engine 经唯一规划点 `SandboxManager` 执行（docs/19），
本模块只处理普通真实工具（read/write/edit/list/grep/web_fetch/memory）。'agent'/'skill'/plan-mode
在 CapabilityRouter / engine 中处理。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import registry
from .shared import _truncate_result
from .spec import TOOLS

# docs/16 #5：handler 表从 spec.TOOLS 派生（单一真相源；host-routed 工具 run=None 不在表内，
# 含 run_shell——它经 SandboxManager 执行，不走通用 handler）。
_HANDLERS = {name: s.run for name, s in TOOLS.items() if s.run is not None}


async def execute_tool(
    name: str, inp: dict, read_file_state: dict[str, float] | None = None
) -> str:
    # ─── read-before-edit + mtime freshness checks ───────────
    if name == "read_file":
        result = _HANDLERS["read_file"](inp)
        if read_file_state is not None and not result.startswith("Error"):
            abs_path = str(Path(inp["file_path"]).resolve())
            try:
                read_file_state[abs_path] = os.path.getmtime(abs_path)
            except OSError:
                pass
        return _truncate_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None:
        abs_path = str(Path(inp["file_path"]).resolve())
        if os.path.exists(abs_path):
            if abs_path not in read_file_state:
                verb = "writing" if name == "write_file" else "editing"
                return f"Error: You must read this file before {verb}. Use read_file first to see its current contents."
            if os.path.getmtime(abs_path) != read_file_state[abs_path]:
                verb = "writing" if name == "write_file" else "editing"
                return f"Warning: {inp['file_path']} was modified externally since your last read. Please read_file again before {verb}."

    # tool_search: activate deferred tools and return their schemas
    if name == "tool_search":
        query = (inp.get("query") or "").lower()
        deferred = [t for t in registry.tool_definitions if t.get("deferred")]
        matches = [
            t for t in deferred
            if query in t["name"].lower() or query in (t.get("description") or "").lower()
        ]
        if not matches:
            return "No matching deferred tools found."
        for m in matches:
            registry._activated_tools.add(m["name"])
        return json.dumps(
            [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"]} for t in matches],
            indent=2,
        )

    handler = _HANDLERS.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    result = _truncate_result(handler(inp))

    # Update mtime after successful write/edit
    if name in ("write_file", "edit_file") and read_file_state is not None and not result.startswith("Error"):
        abs_path = str(Path(inp["file_path"]).resolve())
        try:
            read_file_state[abs_path] = os.path.getmtime(abs_path)
        except OSError:
            pass

    return result
