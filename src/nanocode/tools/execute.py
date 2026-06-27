"""工具调用分发：read-before-edit + mtime 校验、tool_search 激活、结果截断。

shell 执行**不在此**——run_shell 由 engine 经唯一规划点 `SandboxManager` 执行（docs/19），
本模块只处理普通真实工具（read/write/edit/list/grep/web_fetch/memory）。'agent'/'skill'/plan-mode
在 CapabilityRouter / engine 中处理。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .shared import _truncate_result

# docs/24 Phase 2：dispatch 在咽喉点（engine._run_real_tool）按「needs ∩ 信任档策略」铸造
# ToolContext（fs 能力把手）后传入；execute_tool 不再裸调 .run(inp)，而是 .run(ctx, inp)。
# host-routed 工具 run=None，不经此（更早分支截走）。


async def execute_tool(
    name: str,
    inp: dict,
    read_file_state: dict[str, float] | None = None,
    *,
    ctx,
    registry,
) -> str:
    # docs/24 Phase 4a：per-agent overlay registry 经此参数线穿过；ctx 由 dispatch 咽喉点按
    # 当前 sandbox policy 铸造。二者都是执行契约，不在工具层补全。
    # ─── read-before-edit + mtime freshness checks ───────────
    if name == "read_file":
        result = registry.get("read_file").run(ctx, inp)
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
        deferred = [t for t in registry.schemas() if t.get("deferred")]
        matches = [
            t for t in deferred
            if query in t["name"].lower() or query in (t.get("description") or "").lower()
        ]
        if not matches:
            return "No matching deferred tools found."
        for m in matches:
            registry.activate(m["name"])
        return json.dumps(
            [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"]} for t in matches],
            indent=2,
        )

    tool = registry.get(name)
    if tool is None or tool.run is None:
        return f"Unknown tool: {name}"
    raw = tool.run(ctx, inp)
    # run_shell 前台 run 是 async（经 ctx.exec → SandboxManager）；其余 fs/web 工具同步。
    # 统一在此 await 协程，execute_tool 不必关心各 run 的 sync/async 形态。
    if hasattr(raw, "__await__"):
        raw = await raw
    result = _truncate_result(raw)

    # Update mtime after successful write/edit
    if name in ("write_file", "edit_file") and read_file_state is not None and not result.startswith("Error"):
        abs_path = str(Path(inp["file_path"]).resolve())
        try:
            read_file_state[abs_path] = os.path.getmtime(abs_path)
        except OSError:
            pass

    return result
