"""list_files 工具：对齐 Pi 的 `ls`，只列出目录的一层内容。"""

from __future__ import annotations

import os
from pathlib import Path

# 单次返回的默认最大条目数；对齐 Pi 的 ls 默认 limit。
DEFAULT_LIMIT = 500

SCHEMA = {
    "name": "list_files",
    "description": (
        "List directory contents. Returns entries sorted alphabetically, with '/' "
        f"suffix for directories. Includes dotfiles. Defaults to {DEFAULT_LIMIT} entries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list. Defaults to current directory."},
            "limit": {"type": "number", "description": f"Maximum number of entries to return. Defaults to {DEFAULT_LIMIT}."},
        },
        "required": [],
    },
}


def _legacy_pattern_prefix(pattern: str) -> str:
    """Best-effort compatibility for older glob-shaped calls."""
    normalized = pattern.replace("\\", "/").strip()
    if not normalized:
        return "."
    absolute = normalized.startswith("/")
    literal_parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if any(ch in part for ch in "*?["):
            break
        literal_parts.append(part)
    if not literal_parts:
        return os.sep if absolute else "."
    prefix = os.path.join(*literal_parts)
    return os.path.join(os.sep, prefix) if absolute else prefix


def _requested_dir(inp: dict) -> Path:
    base = Path(inp.get("path") or ".")
    pattern = inp.get("pattern")
    if not pattern:
        return base
    prefix = Path(_legacy_pattern_prefix(str(pattern)))
    if prefix == Path("."):
        return base
    if prefix.is_absolute():
        return prefix
    return base / prefix


def run(ctx, inp: dict) -> str:
    try:
        dir_path = _requested_dir(inp)
        raw_limit = inp.get("limit")
        limit = int(DEFAULT_LIMIT if raw_limit is None else raw_limit)

        if not ctx.fs_list.exists(str(dir_path)):
            return f"Error: Path not found: {dir_path}"
        if not ctx.fs_list.is_dir(str(dir_path)):
            return f"Error: Not a directory: {dir_path}"

        try:
            entries = sorted(ctx.fs_list.listdir(str(dir_path)), key=lambda name: name.lower())
        except OSError as e:
            return f"Error: Cannot read directory: {e}"

        results: list[str] = []
        entry_limit_reached = False
        for entry in entries:
            if len(results) >= limit:
                entry_limit_reached = True
                break
            full_path = dir_path / entry
            suffix = ""
            try:
                if ctx.fs_list.is_dir(str(full_path)):
                    suffix = "/"
            except OSError:
                continue
            results.append(entry + suffix)

        if not results:
            return "(empty directory)"
        result = "\n".join(results)
        if entry_limit_reached:
            result += f"\n\n[{limit} entries limit reached. Use limit={limit * 2} for more]"
        return result
    except Exception as e:
        return f"Error: {e}"
