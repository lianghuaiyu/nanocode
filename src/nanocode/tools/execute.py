"""工具调用分发：read-before-edit + mtime 校验、tool_search 激活、结果截断。
'agent' 与 'skill' 工具在 agent.engine 中处理，以避免循环依赖。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import registry
from . import run_shell, sandbox_shell
from .shared import _truncate_result
from .spec import TOOLS

# docs/16 #5：handler 表从 spec.TOOLS 派生（单一真相源；host-routed 工具 run=None 不在表内）。
_HANDLERS = {name: s.run for name, s in TOOLS.items() if s.run is not None}


_SANDBOX_FAIL_HINT = (
    "[sandbox] This command ran in an isolated microVM (no network, python:3.12 "
    "image, only the workspace mounted). If it failed because it needs network "
    "access, host tools (e.g. git, node), or host filesystem access, retry the SAME "
    "command with escalate=true to run it on the host (you will be asked to approve)."
)

# PR-3：原生 OS 沙盒（seatbelt/bwrap）专用的命令失败提示（区别于 microVM 的 _SANDBOX_FAIL_HINT）。
_NATIVE_FAIL_HINT = (
    "[sandbox] This command ran in an OS sandbox (writes confined to the workspace, "
    "no network). If it failed because it needs network access or to write outside "
    "the workspace, retry the SAME command with escalate=true to run it on the host "
    "(you will be asked to approve)."
)


def _format_structured(r: dict, inp: dict) -> str:
    """把 run_structured 的结构化结果格式化为文本，与 run_shell.run 完全一致（避免改 run_shell）。
    注意：仅用于命令失败/成功（error 为 None 时）；机制失败（error 非 None）由调用方单独处理。"""
    if r["timed_out"]:
        return f"Command timed out after {inp.get('timeout', 30000)}ms"
    if r["exit_code"] != 0:
        stderr = f"\nStderr: {r['stderr']}" if r["stderr"] else ""
        stdout = f"\nStdout: {r['stdout']}" if r["stdout"] else ""
        return f"Command failed (exit code {r['exit_code']}){stdout}{stderr}"
    return r["stdout"] or "(no output)"


def _route_run_shell(inp: dict) -> str:
    """前台 run_shell 的统一路由：经 run_shell.plan_shell（单一 planner）决定执行方式。

    kind ∈ {host, blocked, sandbox, microvm}：
    - host    → 原 run_shell 宿主执行（off / 只读 / 已批准提权）。
    - blocked → 返回 escalate 指引文案（fail-closed，绝不静默宿主裸跑）。
    - sandbox → 原生 OS 沙盒 backend.run_structured（workspace-write），区分机制/命令/超时失败。
    - microvm → microVM（sandbox_shell, network=none, 挂载 workspace 读写）。

    无后台旁路：run_in_background 不再早返回裸跑——真到了这里也按前台受限跑（后台由 engine 拦截）。
    """
    kind, info = run_shell.plan_shell(inp, context="foreground")
    if kind == "host":
        return run_shell.run(inp)
    if kind == "blocked":
        # fail-closed：返回 escalate 指引文案，复用 escalate 机制让模型显式重试到宿主。
        return f"[sandbox] {info}"
    if kind == "sandbox":
        # info 是后端模块：取结构化结果区分机制失败（error）/命令失败（exit≠0/超时）/成功。
        r = info.run_structured(inp, posture="workspace-write", cwd=inp.get("_cwd") or os.getcwd())
        if r["error"] is not None:
            # 机制失败（沙盒机器本身坏了）：不静默回退宿主，复用 escalate 让模型显式重试。
            return (
                f"[sandbox] native OS sandbox failed to run this command ({r['error']}). "
                "Retry the SAME command with escalate=true to run it on the host "
                "(you will be asked to approve)."
            )
        if r["timed_out"] or r["exit_code"] != 0:
            # 命令失败（沙盒内正常跑但非零退出/超时）：普通失败，前置原生沙盒专用提示。
            return f"{_NATIVE_FAIL_HINT}\n\n{_format_structured(r, inp)}"
        return r["stdout"] or "(no output)"
    # kind == "microvm"：现有 microVM 路径
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
    if inp.get("_cwd"):
        sinp["_cwd"] = inp["_cwd"]
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
