"""read_file 工具：读取文件内容并附带行号。

docs/15 §9.3：工具边界必须控量,不能只靠后续 compaction。一次大文件读会在 compaction 来得及帮忙
之前就污染上下文。故此处加:
- line cap（默认 2000 行）+ offset/limit 分页（1-based offset）；
- byte cap（硬上限,防超大单行/二进制把上下文撑爆）；
- 清晰的截断标记（告诉模型用 offset/limit 翻页）。
小文件（绝大多数读取）行为不变:逐行 `{n:4d} | line`,无截断标记。
"""

from __future__ import annotations

from pathlib import Path

# 默认行窗口（对齐 Claude Code read_file 的 2000 行语义）。
DEFAULT_LINE_LIMIT = 2000
# 单次读取的硬字节上限（解码前截断;防超大文件/单行/二进制）。
MAX_BYTES = 256 * 1024

SCHEMA = {
    "name": "read_file",
    "description": (
        "Read the contents of a file with line numbers. Large files are paged: by default the "
        "first 2000 lines are returned. Use 'offset' (1-based start line) and 'limit' (max lines) "
        "to read a specific range. A truncation marker indicates when more lines remain."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "The path to the file to read"},
            "offset": {"type": "integer", "description": "1-based line number to start from (default 1)"},
            "limit": {"type": "integer", "description": f"Max lines to read (default {DEFAULT_LINE_LIMIT})"},
        },
        "required": ["file_path"],
    },
}


def _int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def run(inp: dict) -> str:
    try:
        raw = Path(inp["file_path"]).read_bytes()
    except Exception as e:
        return f"Error reading file: {e}"

    byte_truncated = len(raw) > MAX_BYTES
    if byte_truncated:
        raw = raw[:MAX_BYTES]
    content = raw.decode("utf-8", errors="replace")
    lines = content.split("\n")
    total = len(lines)

    offset = max(1, _int(inp.get("offset"), 1))
    limit = _int(inp.get("limit"), DEFAULT_LINE_LIMIT)
    if limit < 1:
        limit = DEFAULT_LINE_LIMIT
    start = offset - 1
    end = start + limit
    window = lines[start:end]

    numbered = "\n".join(f"{start + i + 1:4d} | {line}" for i, line in enumerate(window))

    notes: list[str] = []
    shown_last = start + len(window)
    if end < total:
        notes.append(
            f"[truncated: showing lines {offset}-{shown_last} of {total}; "
            f"use offset/limit to read more]"
        )
    elif offset > 1:
        notes.append(f"[showing lines {offset}-{shown_last} of {total}]")
    if byte_truncated:
        notes.append(f"[file exceeded {MAX_BYTES} bytes; tail omitted — read a later range or open as artifact]")
    if not window and not notes:
        notes.append("[empty range]")
    if notes:
        numbered = (numbered + "\n\n" if numbered else "") + "\n".join(notes)
    return numbered
