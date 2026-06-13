"""记忆召回：轻量头部扫描、新鲜度、语义选取（sideQuery）与异步预取。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..frontmatter import parse_frontmatter
from .store import get_memory_dir, VALID_TYPES, memory_type

# A callable that sends a prompt and returns model text response.
# Signature: async (system: str, user_message: str) -> str
SideQueryFn = Callable[[str, str], Any]  # actually Awaitable[str]


# ─── Memory Header (lightweight scan) ──────────────────────

class MemoryHeader:
    __slots__ = ("filename", "file_path", "mtime_ms", "description", "type")

    def __init__(self, filename: str, file_path: str, mtime_ms: float,
                 description: str | None, type: str | None):
        self.filename = filename
        self.file_path = file_path
        self.mtime_ms = mtime_ms
        self.description = description
        self.type = type


MAX_MEMORY_FILES = 200
MAX_MEMORY_BYTES_PER_FILE = 4096
MAX_SESSION_MEMORY_BYTES = 60 * 1024  # 60KB cumulative per session


def scan_memory_headers() -> list[MemoryHeader]:
    """Scan memory directory — read only frontmatter (first 30 lines) for speed."""
    d = get_memory_dir()
    headers: list[MemoryHeader] = []
    for f in d.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            stat = f.stat()
            raw = f.read_text()
            first30 = "\n".join(raw.split("\n")[:30])
            result = parse_frontmatter(first30)
            meta = result.meta
            t = memory_type(meta)
            headers.append(MemoryHeader(
                filename=f.name,
                file_path=str(f),
                mtime_ms=stat.st_mtime * 1000,
                description=meta.get("description"),
                type=t if t in VALID_TYPES else None,
            ))
        except Exception:
            pass
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """Format manifest for semantic selector: one line per memory."""
    lines = []
    for h in headers:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).isoformat()
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)


# ─── Memory Age / Freshness ────────────────────────────────

def memory_age(mtime_ms: float) -> str:
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def memory_freshness_warning(mtime_ms: float) -> str:
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days <= 1:
        return ""
    return (f"This memory is {days} days old. Memories are point-in-time observations, "
            "not live state — claims about code behavior may be outdated. "
            "Verify against current code before asserting as fact.")


# ─── Semantic Recall (sideQuery) ────────────────────────────

SELECT_MEMORIES_PROMPT = """You are selecting memories that will be useful to an AI coding assistant as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a JSON object with a "selected_memories" array of filenames for the memories that will clearly be useful (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful, do not include it.
- If no memories would clearly be useful, return an empty array."""


class RelevantMemory:
    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str):
        self.path = path
        self.content = content
        self.mtime_ms = mtime_ms
        self.header = header


async def select_relevant_memories(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
) -> list[RelevantMemory]:
    """Call the model to semantically select relevant memories."""
    headers = scan_memory_headers()
    if not headers:
        return []

    candidates = [h for h in headers if h.file_path not in already_surfaced]
    if not candidates:
        return []

    manifest = format_memory_manifest(candidates)

    try:
        text = await side_query(
            SELECT_MEMORIES_PROMPT,
            f"Query: {query}\n\nAvailable memories:\n{manifest}",
        )

        # Extract JSON from response
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []

        parsed = json.loads(match.group(0))
        selected_filenames = set(parsed.get("selected_memories", []))

        selected = [h for h in candidates if h.filename in selected_filenames][:5]

        result: list[RelevantMemory] = []
        for h in selected:
            content = Path(h.file_path).read_text()
            if len(content.encode()) > MAX_MEMORY_BYTES_PER_FILE:
                content = content[:MAX_MEMORY_BYTES_PER_FILE] + "\n\n[... truncated, memory file too large ...]"
            freshness = memory_freshness_warning(h.mtime_ms)
            header_text = (
                f"{freshness}\n\nMemory: {h.file_path}:" if freshness
                else f"Memory (saved {memory_age(h.mtime_ms)}): {h.file_path}:"
            )
            result.append(RelevantMemory(
                path=h.file_path, content=content,
                mtime_ms=h.mtime_ms, header=header_text,
            ))
        return result
    except Exception as e:
        if "cancel" in str(e).lower():
            return []
        return []


# ─── Prefetch Handle ────────────────────────────────────────

class MemoryPrefetch:
    def __init__(self, task: asyncio.Task):
        self.task = task
        self.consumed = False

    @property
    def settled(self) -> bool:
        return self.task.done()


async def _simplemem_prefetch(query, backend, already_surfaced):
    """SimpleMem 检索丢线程池（同步 retrieve 不阻塞事件循环），过滤 already_surfaced。"""
    try:
        hits = await asyncio.to_thread(backend.retrieve, query, limit=5)
    except Exception:
        return []
    return [h for h in (hits or []) if h.path not in already_surfaced]


def start_memory_prefetch(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
    session_memory_bytes: int,
    *,
    backend=None,
) -> MemoryPrefetch | None:
    """Start async memory prefetch. Returns handle to poll for results."""
    # 通用 gate：多词输入 + session 预算（两后端共用）
    if not re.search(r"\s", query.strip()):
        return None

    # Gate: session budget
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None

    # SimpleMem 后端：丢线程池跑同步 retrieve（无需 md 文件存在）
    if backend is not None and getattr(backend, "name", "") == "simplemem":
        task = asyncio.create_task(_simplemem_prefetch(query, backend, already_surfaced))
        return MemoryPrefetch(task)

    # markdown / off / 旧调用：完全沿用现有 markdown 预取逻辑（gate：md 文件存在）
    d = get_memory_dir()
    has_memories = any(f.suffix == ".md" and f.name != "MEMORY.md" for f in d.iterdir())
    if not has_memories:
        return None

    task = asyncio.create_task(
        select_relevant_memories(query, side_query, already_surfaced)
    )
    return MemoryPrefetch(task)


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    """Format recalled memories for injection as user message content."""
    parts = []
    for m in memories:
        parts.append(f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>")
    return "\n\n".join(parts)
