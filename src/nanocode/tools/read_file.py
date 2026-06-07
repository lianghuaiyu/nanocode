"""read_file 工具：读取文件内容并附带行号。"""

from __future__ import annotations

from pathlib import Path

SCHEMA = {
    "name": "read_file",
    "description": "Read the contents of a file. Returns the file content with line numbers.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "The path to the file to read"},
        },
        "required": ["file_path"],
    },
}


def run(inp: dict) -> str:
    try:
        content = Path(inp["file_path"]).read_text()
        lines = content.split("\n")
        numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
        return numbered
    except Exception as e:
        return f"Error reading file: {e}"
