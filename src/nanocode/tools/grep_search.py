"""grep_search 工具：在文件中搜索正则模式（优先系统 grep，回退纯 Python）。"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"

SCHEMA = {
    "name": "grep_search",
    "description": "Search for a pattern in files. Returns matching lines with file paths and line numbers.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The regex pattern to search for"},
            "path": {"type": "string", "description": "Directory or file to search in. Defaults to current directory."},
            "include": {"type": "string", "description": 'File glob pattern to include (e.g., "*.ts", "*.py")'},
        },
        "required": ["pattern"],
    },
}


def run(inp: dict) -> str:
    pattern = inp["pattern"]
    path = inp.get("path") or "."
    include = inp.get("include")

    # Try system grep first (Linux/macOS)
    if not IS_WIN:
        try:
            args = ["grep", "--line-number", "--color=never", "-r"]
            if include:
                args.append(f"--include={include}")
            args.extend(["--", pattern, path])
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 1:
                return "No matches found."
            if result.returncode == 0:
                lines = [l for l in result.stdout.split("\n") if l]
                output = "\n".join(lines[:100])
                if len(lines) > 100:
                    output += f"\n... and {len(lines) - 100} more matches"
                return output
            # Non-zero exit (not 1) — fall through to Python fallback
        except Exception:
            pass  # Fall through to Python fallback

    # Pure Python fallback (Windows, or system grep unavailable)
    return _grep_python(pattern, path, include)


def _grep_python(pattern: str, directory: str, include: str | None) -> str:
    regex = re.compile(pattern)
    include_pattern = include
    matches: list[str] = []

    def walk(d: str) -> None:
        if len(matches) >= 200:
            return
        try:
            entries = os.listdir(d)
        except Exception:
            return
        for name in entries:
            if name.startswith(".") or name == "node_modules":
                continue
            full = os.path.join(d, name)
            if os.path.isdir(full):
                walk(full)
                continue
            if include_pattern and not fnmatch.fnmatch(name, include_pattern):
                continue
            try:
                text = Path(full).read_text(errors="replace")
                for i, line in enumerate(text.split("\n")):
                    if regex.search(line):
                        matches.append(f"{full}:{i+1}:{line}")
                        if len(matches) >= 200:
                            return
            except Exception:
                pass

    walk(directory)
    if not matches:
        return "No matches found."
    output = "\n".join(matches[:100])
    if len(matches) > 100:
        output += f"\n... and {len(matches) - 100} more matches"
    return output
