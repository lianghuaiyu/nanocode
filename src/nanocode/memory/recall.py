"""记忆召回的注入currency（docs/20）。

turn 召回经 `MemoryService.start_prefetch`（no-LLM 快速检索）产出 `RelevantMemory`，
由 `AgentSession.consume_memory_prefetch` 写成 `ContextInjected`。本模块只保留注入所需的
值对象与新鲜度/格式化 helper —— LLM 语义选取已退役（热路径不调 LLM）。
"""

from __future__ import annotations

import asyncio
import time

MAX_MEMORY_BYTES_PER_FILE = 4096
MAX_SESSION_MEMORY_BYTES = 60 * 1024  # 60KB cumulative per session


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


# ─── Injection currency ─────────────────────────────────────

class RelevantMemory:
    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str):
        self.path = path
        self.content = content
        self.mtime_ms = mtime_ms
        self.header = header


class MemoryPrefetch:
    """Handle for an in-flight async prefetch (settle/poll, consumed once)."""

    def __init__(self, task: asyncio.Task):
        self.task = task
        self.consumed = False

    @property
    def settled(self) -> bool:
        return self.task.done()


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    """Format recalled memories for injection as user message content."""
    parts = []
    for m in memories:
        parts.append(f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>")
    return "\n\n".join(parts)
