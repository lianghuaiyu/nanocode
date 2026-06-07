"""list_files 工具：按 glob 模式查找文件路径（只返回文件，不含目录）。"""

from __future__ import annotations

import os
from pathlib import Path

# 单次返回的最大条目数；超出则截断并提示（可在测试中 monkeypatch）。
MAX_RESULTS = 200

SCHEMA = {
    "name": "list_files",
    "description": (
        "Find files by glob pattern. Returns FILE paths only (directories are not "
        "listed) sorted by modification time, most recent first. Use recursive "
        "patterns to explore a tree, e.g. '**/*.py' or 'src/**/*'. For broad, "
        "open-ended exploration, prefer the agent tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": 'Glob pattern to match files (e.g., "**/*.py", "src/**/*")'},
            "path": {"type": "string", "description": "Base directory to search from. Defaults to current directory."},
        },
        "required": ["pattern"],
    },
}


def run(inp: dict) -> str:
    try:
        base = Path(inp.get("path") or ".")
        pattern = inp["pattern"]
        matches: list[tuple[float, str]] = []
        for p in base.glob(pattern):
            if not p.is_file():
                continue
            rel = str(p.relative_to(base) if base != Path(".") else p)
            # 跳过 node_modules 和 .git
            if "node_modules" in rel or ".git" in rel.split(os.sep):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            matches.append((mtime, rel))

        if not matches:
            return "No files found matching the pattern."

        # 按修改时间排序，最新在前
        matches.sort(key=lambda m: m[0], reverse=True)
        shown = [rel for _, rel in matches[:MAX_RESULTS]]
        result = "\n".join(shown)
        if len(matches) > MAX_RESULTS:
            result += f"\n... and {len(matches) - MAX_RESULTS} more (narrow your pattern or path)"
        return result
    except Exception as e:
        return f"Error listing files: {e}"
