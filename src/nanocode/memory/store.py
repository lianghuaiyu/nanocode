"""记忆存储：目录解析、四类记忆 CRUD 与 MEMORY.md 索引维护。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..frontmatter import parse_frontmatter, format_frontmatter
from ..paths import project_memory_dir

VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000


class MemoryEntry:
    __slots__ = ("name", "description", "type", "filename", "content")

    def __init__(self, name: str, description: str, type: str, filename: str, content: str):
        self.name = name
        self.description = description
        self.type = type
        self.filename = filename
        self.content = content


# ─── Paths ──────────────────────────────────────────────────


def get_memory_dir() -> Path:
    return project_memory_dir()


def _get_index_path() -> Path:
    return get_memory_dir() / "MEMORY.md"


# ─── Slugify ────────────────────────────────────────────────


def _slugify(text: str) -> str:
    # 保留 Unicode 字母/数字（含中文等非 ASCII），其余折叠为下划线。
    # 仅用 ASCII [a-z0-9] 会把纯中文名 slug 成空串 → 文件名退化为 "{type}_.md" 互相覆盖。
    s = re.sub(r"[^\w]+", "_", text.lower(), flags=re.UNICODE).strip("_")
    if not s:
        # 极端情况（如纯标点/emoji）：用短 hash 兜底，保证文件名唯一、不丢数据。
        s = "mem_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return s[:40]


# ─── CRUD ───────────────────────────────────────────────────


def memory_type(meta: dict) -> str | None:
    """先读嵌套 metadata.type，回退顶层 type（兼容扁平旧文件）。"""
    md = meta.get("metadata")
    if isinstance(md, dict) and md.get("type"):
        return md.get("type")
    return meta.get("type")


def list_memories() -> list[MemoryEntry]:
    d = get_memory_dir()
    entries: list[MemoryEntry] = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            result = parse_frontmatter(f.read_text())
            meta = result.meta
            mtype = memory_type(meta)
            if not meta.get("name") or not mtype:
                continue
            t = mtype if mtype in VALID_TYPES else "project"
            entries.append(MemoryEntry(
                name=meta["name"],
                description=meta.get("description", ""),
                type=t,
                filename=f.name,
                content=result.body,
            ))
        except Exception:
            pass
    # Sort by mtime desc
    entries.sort(key=lambda e: (d / e.filename).stat().st_mtime, reverse=True)
    return entries


def save_memory(name: str, description: str, type: str, content: str) -> str:
    d = get_memory_dir()
    filename = f"{type}_{_slugify(name)}.md"
    text = format_frontmatter({"name": name, "description": description, "type": type}, content)
    (d / filename).write_text(text)
    _update_memory_index()
    return filename


def delete_memory(filename: str) -> bool:
    filepath = get_memory_dir() / filename
    if not filepath.exists():
        return False
    filepath.unlink()
    _update_memory_index()
    return True


# ─── Index ──────────────────────────────────────────────────


def _update_memory_index() -> None:
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories:
        lines.append(f"- **[{m.name}]({m.filename})** ({m.type}) — {m.description}")
    _get_index_path().write_text("\n".join(lines))


def load_memory_index() -> str:
    index_path = _get_index_path()
    if not index_path.exists():
        return ""
    content = index_path.read_text()
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated, too many memory entries ...]"
    if len(content.encode()) > MAX_INDEX_BYTES:
        content = content[:MAX_INDEX_BYTES] + "\n\n[... truncated, index too large ...]"
    return content
