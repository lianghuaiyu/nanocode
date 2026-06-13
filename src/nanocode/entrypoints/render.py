"""render —— 客户端侧的**领域渲染**（docs/17：把工具名→标题/摘要、结果摘要、文件改动解析等
agent 领域知识从通用 tui 框架里剥出来，放到 client 这一层）。

对位 Pi 的 `modes/interactive/components`：通用渲染框架（`nanocode/tui.py`）零领域知识，
「一个 read_file 工具调用长什么样」这类知识属于客户端。本模块是纯函数 + 薄渲染编排，
依赖 tui 通用原语（print_bullet / print_connector / print_diff），被 TerminalClient 调用。
"""

from __future__ import annotations

import re

from .. import tui

# 工具名 → 显示标题（领域知识）。未登记的工具用原名。
_TOOL_TITLES = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Update",
    "list_files": "List",
    "grep_search": "Grep",
    "run_shell": "Bash",
    "sandbox_shell": "Sandbox",
    "skill": "Skill",
    "agent": "Task",
}


def _tool_summary(name: str, inp: dict) -> str:
    if name in ("read_file", "write_file", "edit_file"):
        return inp.get("file_path", "")
    if name == "list_files":
        return inp.get("pattern", "")
    if name == "grep_search":
        return f'"{inp.get("pattern", "")}" in {inp.get("path", ".")}'
    if name == "run_shell":
        cmd = inp.get("command", "")
        return cmd[:60] + "..." if len(cmd) > 60 else cmd
    if name == "sandbox_shell":
        cmd = inp.get("command", "")
        image = inp.get("image", "python:3.12")
        summary = cmd[:60] + "..." if len(cmd) > 60 else cmd
        return f"[{image}] {summary}"
    if name == "skill":
        return inp.get("skill_name", "")
    if name == "agent":
        return f'[{inp.get("type", "general")}] {inp.get("description", "")}'
    return ""


def _result_summary(name: str, result: str) -> str:
    r = result or ""
    first = r.split("\n", 1)[0].strip()
    if r.startswith(("Error", "Command failed", "Command timed out")):
        return first or "error"
    if name == "read_file":
        return f"Read {(r.count(chr(10)) + 1) if r else 0} lines"
    if name == "grep_search":
        if r.startswith("No matches"):
            return "No matches"
        n = len([l for l in r.split("\n") if l and not l.startswith("... and ")])
        return f"{n} matches"
    if name == "list_files":
        if r.startswith("No files"):
            return "No files"
        n = len([l for l in r.split("\n") if l and not l.startswith("... and ")])
        return f"{n} files"
    if name in ("run_shell", "sandbox_shell"):
        if r.strip() == "(no output)":
            return "(no output)"
        for l in r.split("\n"):
            if l.strip():
                return l.strip()[:80]
        return "(no output)"
    return first[:80] or "done"


def print_tool_call(name: str, inp: dict) -> None:
    """工具调用回显：领域算出 title/summary，交 tui 通用 bullet 行渲染。"""
    tui.print_bullet(_TOOL_TITLES.get(name, name), _tool_summary(name, inp))


def print_tool_result(name: str, result: str) -> None:
    """工具结果回显：edit/write 成功 → 解析成 diff 块；否则一行摘要。"""
    if name in ("edit_file", "write_file") and not result.startswith("Error"):
        lines = result.split("\n")
        body = lines[1:]
        adds = sum(1 for l in body if l.startswith("+ "))
        dels = sum(1 for l in body if l.startswith("- "))
        if name == "write_file" and adds == 0 and dels == 0:
            m = re.search(r"\((\d+) lines?\)", lines[0])
            adds = int(m.group(1)) if m else 0
        tui.print_diff(adds, dels, body)
        return
    tui.print_connector(_result_summary(name, result))
