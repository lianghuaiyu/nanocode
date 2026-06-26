"""write_file 工具：写入文件，并在写入记忆目录时自动刷新 MEMORY.md 索引。"""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "The path to the file to write"},
            "content": {"type": "string", "description": "The content to write to the file"},
        },
        "required": ["file_path", "content"],
    },
}


def _auto_update_memory_index(file_path: str) -> None:
    # HOST-WRITE EXEMPTION (docs/24 Phase 2)：MEMORY.md 是宿主配置派生的固定路径（get_memory_dir()），
    # **非模型入参**，故不经 ctx.fs_write 的模型路径沙箱约束——保持裸写，让记忆索引继续工作。
    try:
        from ..memory import get_memory_dir
        mem_dir = str(get_memory_dir())
        if file_path.startswith(mem_dir) and file_path.endswith(".md") and not file_path.endswith("MEMORY.md"):
            mem_path = Path(mem_dir)
            lines = ["# Memory Index", ""]
            for f in sorted(mem_path.glob("*.md")):
                if f.name == "MEMORY.md":
                    continue
                try:
                    raw = f.read_text()
                    name_match = re.search(r"^name:\s*(.+)$", raw, re.MULTILINE)
                    type_match = re.search(r"^type:\s*(.+)$", raw, re.MULTILINE)
                    desc_match = re.search(r"^description:\s*(.+)$", raw, re.MULTILINE)
                    if name_match and type_match:
                        n = name_match.group(1).strip()
                        t = type_match.group(1).strip()
                        d = desc_match.group(1).strip() if desc_match else ""
                        lines.append(f"- **[{n}]({f.name})** ({t}) — {d}")
                except Exception:
                    pass
            (mem_path / "MEMORY.md").write_text("\n".join(lines))
    except Exception:
        pass


def run(ctx, inp: dict) -> str:
    try:
        path = inp["file_path"]
        parent = str(Path(path).parent)
        ctx.fs_write.mkdir(parent, parents=True, exist_ok=True)
        ctx.fs_write.write_text(path, inp["content"])
        _auto_update_memory_index(str(Path(path)))
        lines = inp["content"].split("\n")
        line_count = len(lines)
        preview = "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines[:30]))
        trunc = f"\n  ... ({line_count} lines total)" if line_count > 30 else ""
        return f"Successfully wrote to {inp['file_path']} ({line_count} lines)\n\n{preview}{trunc}"
    except Exception as e:
        return f"Error writing file: {e}"
