"""memory/markdown_backend.py — markdown + off backends (docs/20 §5.2).

Implements the explicit `MemoryBackend` protocol over the file-based markdown
store. `retrieve_fast` is deterministic keyword ranking (no LLM) — it is the
turn hot path and the `memory search` action.

`MarkdownMemoryBackend` is an *explicit* backend, never a silent fallback for
an unavailable SimpleMem (docs/20 §2.4 / §10).
"""
from __future__ import annotations

import time

from .models import (
    MemoryHit, MemoryListResult, MemoryReadResult, MemoryWriteResult, MemoryEntryView,
)
from .store import (
    list_memories, save_memory, get_memory_dir, load_memory_index, VALID_TYPES,
    _update_memory_index,
)
from .maintenance import archive_file


def _score(query: str, entry) -> int:
    """Deterministic keyword score: name×3 + description×2 + content×1."""
    q = query.lower().split()
    if not q:
        return 0
    name = (entry.name or "").lower()
    desc = (entry.description or "").lower()
    body = (entry.content or "").lower()
    score = 0
    for w in q:
        score += 3 * name.count(w) + 2 * desc.count(w) + body.count(w)
    return score


class MarkdownMemoryBackend:
    name = "markdown"

    def retrieve_fast(self, query: str, *, limit: int = 5, token_budget: int = 0) -> list[MemoryHit]:
        if not query.strip():
            return []
        entries = list_memories()
        if not entries:
            return []
        scored = [(e, _score(query, e)) for e in entries]
        hits = sorted((p for p in scored if p[1] > 0), key=lambda p: p[1], reverse=True)
        d = get_memory_dir()
        out: list[MemoryHit] = []
        for e, s in hits[:limit]:
            fp = d / e.filename
            try:
                mtime_ms = fp.stat().st_mtime * 1000
            except OSError:
                mtime_ms = time.time() * 1000
            out.append(MemoryHit(ref=str(fp), title=e.name, content=e.content,
                                 kind=e.type, score=float(s), mtime_ms=mtime_ms))
        return out

    def list(self, *, cursor: "str | None" = None, limit: int = 50) -> MemoryListResult:
        entries = list_memories()
        views = [MemoryEntryView(ref=e.filename, title=e.name, kind=e.type,
                                 description=e.description) for e in entries[:limit]]
        return MemoryListResult(entries=views, cursor=None, total=len(entries))

    def read(self, ref: str, *, max_bytes: int = 8192) -> MemoryReadResult:
        # Accept either a bare filename or an absolute path under the memory dir.
        d = get_memory_dir()
        name = ref.rsplit("/", 1)[-1]
        path = d / name
        if not path.exists():
            return MemoryReadResult(ref=ref, content="", found=False)
        content = path.read_text()
        truncated = False
        if len(content.encode()) > max_bytes:
            content = content[:max_bytes]
            truncated = True
        return MemoryReadResult(ref=ref, content=content, found=True, truncated=truncated)

    def add_note(self, *, title: str, kind: str, content: str, metadata: dict) -> MemoryWriteResult:
        mtype = kind if kind in VALID_TYPES else "project"
        fn = save_memory(title, (metadata or {}).get("description", ""), mtype, content)
        return MemoryWriteResult(ref=fn, ok=True, detail=f"saved as {mtype}")

    def update(self, ref: str, *, content: "str | None", metadata: "dict | None") -> MemoryWriteResult:
        name = ref.rsplit("/", 1)[-1]
        entries = {e.filename: e for e in list_memories()}
        e = entries.get(name)
        if e is None:
            return MemoryWriteResult(ref=ref, ok=False, detail="unknown memory")
        new_content = content if content is not None else e.content
        new_desc = (metadata or {}).get("description", e.description)
        save_memory(e.name, new_desc, e.type, new_content)
        return MemoryWriteResult(ref=name, ok=True, detail="updated")

    def archive(self, ref: str, *, reason: str) -> MemoryWriteResult:
        name = ref.rsplit("/", 1)[-1]
        ok = archive_file(name, reason=reason or "archived via memory tool")
        if not ok:
            return MemoryWriteResult(ref=ref, ok=False, detail="unknown memory")
        _update_memory_index()
        return MemoryWriteResult(ref=name, ok=True, detail="archived (recoverable)")

    def stats(self) -> dict:
        return {"backend": self.name, "count": len(list_memories()),
                "dir": str(get_memory_dir())}

    # backend-specific helper used by the backend-aware prompt (docs/20 §3 Phase 3)
    def memory_index(self) -> str:
        return load_memory_index()


class OffMemoryBackend:
    name = "off"

    def retrieve_fast(self, query: str, *, limit: int = 5, token_budget: int = 0) -> list[MemoryHit]:
        return []

    def list(self, *, cursor: "str | None" = None, limit: int = 50) -> MemoryListResult:
        return MemoryListResult()

    def read(self, ref: str, *, max_bytes: int = 8192) -> MemoryReadResult:
        return MemoryReadResult(ref=ref, content="", found=False)

    def add_note(self, *, title: str, kind: str, content: str, metadata: dict) -> MemoryWriteResult:
        return MemoryWriteResult.unsupported_op("add_note", self.name)

    def update(self, ref: str, *, content: "str | None", metadata: "dict | None") -> MemoryWriteResult:
        return MemoryWriteResult.unsupported_op("update", self.name)

    def archive(self, ref: str, *, reason: str) -> MemoryWriteResult:
        return MemoryWriteResult.unsupported_op("archive", self.name)

    def stats(self) -> dict:
        return {"backend": self.name}
