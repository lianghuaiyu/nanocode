"""tui/tooltext.py —— 工具调用的纯文案派生(领域→展示)。

对位 Pi 的 per-tool 渲染器,但保持 **纯函数、零 agent 依赖**(嵌入式边界):工具名→标题、
参数→摘要、结果→摘要、edit/write 结果→diff 解析。被 `rich_app._render_tool_box` 调用。
逻辑与 `entrypoints/render.py`(headless 客户端的领域渲染)一致,刻意复制以不跨边界 import。
"""

from __future__ import annotations

import re

# 工具名 → 显示标题。未登记的工具用原名。
TOOL_TITLES = {
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

# 输出体应直接预览(而非一行摘要)的工具:命令/构建输出是用户真正想看的。
_SHOW_OUTPUT = {"run_shell", "sandbox_shell"}


def tool_title(name: str) -> str:
    return TOOL_TITLES.get(name, name)


def tool_summary(name: str, inp: dict) -> str:
    inp = inp or {}
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


def is_error_result(result: str) -> bool:
    return (result or "").startswith(("Error", "Command failed", "Command timed out"))


def result_summary(name: str, result: str) -> str:
    r = result or ""
    first = r.split("\n", 1)[0].strip()
    if is_error_result(r):
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


def wants_output_preview(name: str) -> bool:
    """该工具是否应预览多行输出(bash/sandbox)而非一行摘要。"""
    return name in _SHOW_OUTPUT


def output_lines(result: str, limit: int = 12) -> tuple[list[str], int]:
    """非空输出行的前 limit 行 + 余量计数(供 '… (+N lines)')。"""
    nonempty = [l for l in (result or "").split("\n") if l.strip()]
    return nonempty[:limit], max(0, len(nonempty) - limit)


def parse_diff(name: str, result: str) -> tuple[int, int, list[str]] | None:
    """edit/write 成功结果 → (adds, dels, body_lines)。非 diff/错误 → None。

    结果约定:首行摘要,其后 body 行以 '+ ' / '- ' / 上下文。write_file 无 +/- 时从
    首行 '(N lines)' 推 adds(对位 `entrypoints/render.py:print_tool_result`)。"""
    if name not in ("edit_file", "write_file") or is_error_result(result):
        return None
    lines = (result or "").split("\n")
    body = lines[1:]
    adds = sum(1 for l in body if l.startswith("+ "))
    dels = sum(1 for l in body if l.startswith("- "))
    if name == "write_file" and adds == 0 and dels == 0:
        m = re.search(r"\((\d+) lines?\)", lines[0] if lines else "")
        adds = int(m.group(1)) if m else 0
    return adds, dels, body
