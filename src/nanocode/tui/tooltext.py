"""tui/tooltext.py —— 工具调用的纯文案派生(领域→展示)。

对位 Pi 的 per-tool 渲染器,但保持 **纯函数、零 agent 依赖**(嵌入式边界):工具名→标题、
参数→摘要、结果→摘要、edit/write 结果→diff 解析。被 `rich_app._render_tool_box` 调用。
逻辑与 `entrypoints/render.py`(headless 客户端的领域渲染)一致,刻意复制以不跨边界 import。
"""

from __future__ import annotations

# 工具名 → 显示标题。未登记的工具用原名。
TOOL_TITLES = {
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "list_files": "ls",
    "grep_search": "grep",
    "run_shell": "$",
    "skill": "Skill",
    "agent": "Task",
}

def tool_title(name: str) -> str:
    return TOOL_TITLES.get(name, name)


def _legacy_list_path(inp: dict) -> str:
    path = inp.get("path") or "."
    pattern = str(inp.get("pattern") or "").replace("\\", "/").strip()
    if not pattern:
        return path
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


def tool_summary(name: str, inp: dict) -> str:
    inp = inp or {}
    if name in ("read_file", "write_file", "edit_file"):
        return inp.get("file_path", "")
    if name == "list_files":
        return _legacy_list_path(inp)
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
    return ""


def input_preview_lines(name: str, inp: dict, *, expanded: bool) -> tuple[list[str], int, int]:
    """Pi-style call-body preview for tools whose input is the useful payload."""
    if name != "write_file":
        return [], 0, 0
    content = inp.get("content")
    if not isinstance(content, str) or not content:
        return [], 0, 0
    lines = content.replace("\t", "   ").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    max_lines = len(lines) if expanded else 10
    shown = lines[:max_lines]
    return shown, max(0, len(lines) - len(shown)), len(lines)


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


def suppress_success_result(name: str, result: str) -> bool:
    """Pi's write renderer shows the written content in the call and hides success text."""
    return name == "write_file" and not is_error_result(result)


def preview_limit(name: str, *, is_error: bool = False) -> int | None:
    """Pi-style collapsed result line limits. None means hide result in collapsed mode."""
    if name in ("run_shell",):
        return 5
    if name == "list_files":
        return 20
    if name == "grep_search":
        return 15
    if name == "read_file":
        return 10 if is_error else None
    return None


def preview_from_tail(name: str) -> bool:
    """Bash-style output previews show the tail, matching Pi's visual truncation."""
    return name in ("run_shell",)


def output_lines(result: str, limit: int | None = 12, *, tail: bool = False) -> tuple[list[str], int]:
    """非空输出行的前/后 limit 行 + 余量计数；limit=None 表示完整输出。"""
    nonempty = [l for l in (result or "").split("\n") if l.strip()]
    if limit is None:
        return nonempty, 0
    if tail:
        return nonempty[-limit:], max(0, len(nonempty) - limit)
    return nonempty[:limit], max(0, len(nonempty) - limit)


def parse_diff(name: str, result: str) -> tuple[int, int, list[str]] | None:
    """edit 成功结果 → (adds, dels, body_lines)。非 diff/错误 → None。

    结果约定:首行摘要,其后 body 行以 '+ ' / '- ' / 上下文。write_file 对齐 Pi:
    成功内容在调用块预览,结果不重复展示。"""
    if name != "edit_file" or is_error_result(result):
        return None
    lines = (result or "").split("\n")
    body = lines[1:]
    adds = sum(1 for l in body if l.startswith("+ "))
    dels = sum(1 for l in body if l.startswith("- "))
    return adds, dels, body
