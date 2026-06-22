"""render —— 客户端侧的**领域渲染**（docs/17：把工具名→标题/摘要、结果摘要、文件改动解析等
agent 领域知识从通用 tui 框架里剥出来，放到 client 这一层）。

对位 Pi 的 `modes/interactive/components`：通用渲染框架（`nanocode/tui.py`）零领域知识，
「一个 read_file 工具调用长什么样」这类知识属于客户端。本模块是纯函数 + 薄渲染编排，
依赖 tui 通用原语（print_bullet / print_connector / print_diff），被 TerminalClient 调用。
"""

from __future__ import annotations

import json

from .. import tui

_TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked", "cancelled", "lost", "timed_out"}

# 工具名 → 显示标题（领域知识）。未登记的工具用原名。
_TOOL_TITLES = {
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "list_files": "ls",
    "grep_search": "grep",
    "run_shell": "$",
    "skill": "Skill",
    "agent": "Task",
    "run_list": "runs",
    "run_status": "Sub-agent status",
    "run_output": "Sub-agent output",
    "get_subagent_result": "Sub-agent result",
    "run_cancel": "cancel",
    "run_send": "steer",
}


def _tool_summary(name: str, inp: dict) -> str:
    if name in ("read_file", "write_file", "edit_file"):
        return inp.get("file_path", "")
    if name == "list_files":
        path = inp.get("path") or "."
        pattern = str(inp.get("pattern") or "").replace("\\", "/").strip()
        absolute = pattern.startswith("/")
        parts: list[str] = []
        for part in pattern.split("/"):
            if part in ("", "."):
                continue
            if any(ch in part for ch in "*?["):
                break
            parts.append(part)
        if not parts:
            return path
        prefix = "/".join(parts)
        if path in ("", "."):
            return f"/{prefix}" if absolute else prefix
        return f"{path.rstrip('/')}/{prefix}"
    if name == "grep_search":
        glob = inp.get("glob") or inp.get("include")
        suffix = f" ({glob})" if glob else ""
        return f'/{inp.get("pattern", "")}/ in {inp.get("path", ".")}{suffix}'
    if name == "run_shell":
        cmd = inp.get("command", "")
        timeout = inp.get("timeout")
        suffix = f" (timeout {timeout / 1000:g}s)" if isinstance(timeout, (int, float)) else ""
        return f"{cmd}{suffix}"
    if name == "skill":
        return inp.get("skill_name", "")
    if name == "agent":
        return f'[{inp.get("type", "general")}] {inp.get("description", "")}'
    if name == "run_list":
        return inp.get("status", "")
    if name in ("run_status", "run_output", "get_subagent_result", "run_cancel", "run_send"):
        return inp.get("child_session_id", "")
    return ""


def _result_summary(name: str, result: str) -> str:
    r = result or ""
    first = r.split("\n", 1)[0].strip()
    if r.startswith(("Error", "Command failed", "Command timed out")):
        return first or "error"
    if name == "run_list":
        try:
            runs = json.loads(r)
        except Exception:
            return first[:80] or "done"
        if isinstance(runs, list):
            return f"{len(runs)} runs"
    if name == "run_status":
        summary = _run_status_summary(r)
        if summary:
            return summary
    if name in ("run_output", "get_subagent_result"):
        summary = _run_output_summary(name, r)
        if summary:
            return summary
    if name == "read_file":
        return f"Read {(r.count(chr(10)) + 1) if r else 0} lines"
    if name == "grep_search":
        if r.startswith("No matches"):
            return "No matches"
        n = len([l for l in r.split("\n") if l and not l.startswith("... and ")])
        return f"{n} matches"
    if name == "list_files":
        if r.startswith("(empty directory)"):
            return "empty directory"
        n = len([l for l in r.split("\n") if l and not l.startswith("[")])
        return f"{n} entries"
    if name in ("run_shell",):
        if r.strip() == "(no output)":
            return "(no output)"
        for l in r.split("\n"):
            if l.strip():
                return l.strip()[:80]
        return "(no output)"
    return first[:80] or "done"


def _run_status_summary(result: str) -> str:
    try:
        data = json.loads(result)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    status = data.get("status") or "unknown"
    child = data.get("child_session_id") or data.get("run_id") or data.get("childSessionId") or ""
    metrics = data.get("metrics") or {}
    tools = metrics.get("toolUses")
    active = metrics.get("currentTool")
    parts = ["Sub-agent status", str(child), str(status)]
    if active:
        parts.append(f"tool {active}")
    elif tools is not None:
        parts.append(f"{tools} tools")
    return " · ".join(p for p in parts if p)


def _first_text_line(value: object, *, limit: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        text = line.strip()
        if text:
            return text[:limit]
    return ""


def _run_output_summary(name: str, result: str) -> str:
    try:
        data = json.loads(result)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    label = "Sub-agent result" if name == "get_subagent_result" else "Sub-agent output"
    status = data.get("status") or "unknown"
    child = data.get("childSessionId") or data.get("runId") or ""
    summary = (
        _first_text_line(data.get("summary"))
        or _first_text_line(data.get("error"))
        or _first_text_line(data.get("result"))
    )
    if status not in _TERMINAL_RUN_STATUSES and not summary:
        summary = "not ready"
    parts = [label, str(child), str(status)]
    if summary:
        parts.append(summary)
    return " · ".join(p for p in parts if p)


def print_tool_call(name: str, inp: dict) -> None:
    """工具调用回显：领域算出 title/summary，交 tui 通用 bullet 行渲染。"""
    if name == "run_shell":
        tui.print_bullet(f"$ {_tool_summary(name, inp) or '...'}")
        return
    tui.print_bullet(_TOOL_TITLES.get(name, name), _tool_summary(name, inp))


def print_tool_result(name: str, result: str) -> None:
    """工具结果回显：edit/write 成功 → 解析成 diff 块；否则一行摘要。"""
    if name == "write_file" and not result.startswith("Error"):
        return
    if name == "edit_file" and not result.startswith("Error"):
        lines = result.split("\n")
        body = lines[1:]
        adds = sum(1 for l in body if l.startswith("+ "))
        dels = sum(1 for l in body if l.startswith("- "))
        tui.print_diff(adds, dels, body)
        return
    tui.print_connector(_result_summary(name, result))
