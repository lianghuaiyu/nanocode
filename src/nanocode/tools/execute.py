"""工具调用分发：read-before-edit + mtime 校验、tool_search 激活、结果截断。
'agent' 与 'skill' 工具在 agent.engine 中处理，以避免循环依赖。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import registry
from .shared import _truncate_result
from . import read_file, write_file, edit_file, list_files, grep_search, run_shell, sandbox_shell, web_fetch, memory_tool

_HANDLERS = {
    "read_file": read_file.run, "write_file": write_file.run,
    "edit_file": edit_file.run, "list_files": list_files.run,
    "grep_search": grep_search.run, "run_shell": run_shell.run,
    "sandbox_shell": sandbox_shell.run,
    "web_fetch": web_fetch.run,
    "memory": memory_tool.run,
}


_SANDBOX_FAIL_HINT = (
    "[sandbox] This command ran in an isolated microVM (no network, python:3.12 "
    "image, only the workspace mounted). If it failed because it needs network "
    "access, host tools (e.g. git, node), or host filesystem access, retry the SAME "
    "command with escalate=true to run it on the host (you will be asked to approve)."
)


def _route_run_shell(inp: dict) -> str:
    """前台 run_shell 的 host/sandbox 路由 + 沙盒失败时的提权提示（实验性沙盒层）。
    sandbox 归类且 msb 可用时，转交 sandbox_shell（network=none, 挂载 workspace 读写）；
    否则（含归类为 host、后台、已批准提权、msb 不可用）走原 run_shell 宿主执行。"""
    from . import permissions
    if inp.get("run_in_background"):
        return run_shell.run(inp)
    if inp.get("escalate"):
        return run_shell.run(inp)  # 已批准的提权：在真实宿主执行
    if permissions.classify_shell_runtime(inp.get("command", "")) != "sandbox":
        return run_shell.run(inp)
    if sandbox_shell._resolve_msb() is None:
        return run_shell.run(inp)  # 优雅回退：本机无 microsandbox
    sinp = {
        "command": inp.get("command"),
        "network": "none",
        "mount_workspace": True,
        "deps": "none",
    }
    if inp.get("timeout"):
        sinp["timeout_ms"] = int(inp["timeout"])
    if inp.get("_session_id"):
        sinp["_session_id"] = inp["_session_id"]
    result = sandbox_shell.run(sinp)
    if result.startswith(("Command failed", "Command timed out")):
        return f"{_SANDBOX_FAIL_HINT}\n\n{result}"
    return result


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
    if name == "run_shell":
        result = _truncate_result(_route_run_shell(inp))
    else:
        result = _truncate_result(handler(inp))

    # Update mtime after successful write/edit
    if name in ("write_file", "edit_file") and read_file_state is not None and not result.startswith("Error"):
        abs_path = str(Path(inp["file_path"]).resolve())
        try:
            read_file_state[abs_path] = os.path.getmtime(abs_path)
        except OSError:
            pass

    return result
