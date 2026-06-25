"""memory/simplemem_backend.py — adapts MemoryService ↔ SimpleMemEngine (docs/20 §5.2).

Implements the `MemoryBackend` protocol over the nanocode-owned SimpleMemEngine.
The host injects llm/embed callables and the data root; this module resolves the
project-scoped store path and translates engine models to host models.
Unsupported write ops (update/archive) return an explicit unsupported result —
never a markdown fallback (docs/20 §2.4).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .models import (
    MemoryHit, MemoryListResult, MemoryReadResult, MemoryWriteResult, MemoryEntryView,
)
from .engines.simplemem import (
    SimpleMemConfig, MemoryNote, create_simplemem_engine,
)

_REF_PREFIX = "simplemem://"


def _project_hash(cwd: str) -> str:
    return hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]


class SimpleMemBackend:
    name = "simplemem"

    def __init__(self, engine) -> None:
        self._engine = engine

    @property
    def engine(self):
        return self._engine

    # ── read ──────────────────────────────────────────────────────────
    def retrieve_fast(self, query: str, *, limit: int = 5, token_budget: int = 0) -> list[MemoryHit]:
        entries = self._engine.retrieve_fast(query, limit=limit)
        return [self._hit(e) for e in entries]

    def list(self, *, cursor: "str | None" = None, limit: int = 50) -> MemoryListResult:
        page = self._engine.list_entries(limit=limit, cursor=cursor)
        views = [MemoryEntryView(ref=_REF_PREFIX + e.entry_id,
                                 title=(e.topic or e.lossless_restatement[:60]),
                                 kind="", description=e.lossless_restatement[:80])
                 for e in page.entries]
        return MemoryListResult(entries=views, cursor=page.cursor, total=page.total)

    def read(self, ref: str, *, max_bytes: int = 8192) -> MemoryReadResult:
        entry_id = ref[len(_REF_PREFIX):] if ref.startswith(_REF_PREFIX) else ref
        e = self._engine.read_entry(entry_id)
        if e is None:
            return MemoryReadResult(ref=ref, content="", found=False)
        parts = [e.lossless_restatement]
        if e.keywords:
            parts.append("keywords: " + ", ".join(e.keywords))
        if e.timestamp:
            parts.append("time: " + e.timestamp)
        if e.persons:
            parts.append("persons: " + ", ".join(e.persons))
        content = "\n".join(parts)
        truncated = len(content.encode()) > max_bytes
        if truncated:
            content = content[:max_bytes]
        return MemoryReadResult(ref=ref, content=content, found=True, truncated=truncated)

    # ── write ─────────────────────────────────────────────────────────
    def add_note(self, *, title: str, kind: str, content: str, metadata: dict) -> MemoryWriteResult:
        entry = self._engine.add_note(MemoryNote(title=title, content=content, kind=kind,
                                                 metadata=metadata or {}))
        return MemoryWriteResult(ref=_REF_PREFIX + entry.entry_id, ok=True, detail="added")

    def update(self, ref: str, *, content: "str | None", metadata: "dict | None") -> MemoryWriteResult:
        return MemoryWriteResult.unsupported_op("update", self.name)

    def archive(self, ref: str, *, reason: str) -> MemoryWriteResult:
        return MemoryWriteResult.unsupported_op("archive", self.name)

    def stats(self) -> dict:
        return self._engine.stats()

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _hit(e) -> MemoryHit:
        content = e.lossless_restatement
        if e.keywords:
            content = f"{content}\n(keywords: {', '.join(e.keywords)})"
        return MemoryHit(ref=_REF_PREFIX + e.entry_id,
                         title=(e.topic or e.lossless_restatement[:60]),
                         content=content, kind="", timestamp=e.timestamp, mtime_ms=0.0)


def create_simplemem_backend(*, cwd: str, agent_dir: str, llm=None, embed=None) -> SimpleMemBackend:
    """Build a project-scoped SimpleMem backend (docs/20 §6.6 / docs/22 Phase 1).

    `embed` is `(embed_fn, dim)` or None — required, fails loud if absent.
    `llm` is callable(messages)->str or None (needed only for generation).
    Store root: {agent_dir}/memory/simplemem/{project_hash}. The promoted
    RetrievalConfig is loaded from that store root and injected into the engine
    config; a malformed config fails loud here (no silent default, no markdown
    fallback — docs/22 §9.1.10)."""
    if embed is None:
        raise RuntimeError(
            "SimpleMem requires an embeddings endpoint. Set "
            "NANOCODE_MEMORY_EMBED_BASE_URL / _API_KEY / _MODEL / _DIM.")
    embed_fn, dim = embed
    root = str(Path(agent_dir) / "memory" / "simplemem" / _project_hash(cwd))
    # The promoted retrieval config lives at the RESOLVED store root (the same
    # path the engine's vector store uses). Resolve once, deterministically.
    from .engines.simplemem.migrations import resolve_scoped_root
    from .retrieval_config_store import load_retrieval_config
    resolved_root = str(resolve_scoped_root(root, data_root=agent_dir))
    retrieval = load_retrieval_config(resolved_root)
    config = SimpleMemConfig(root=root, embed_dimension=dim, retrieval=retrieval)
    engine = create_simplemem_engine(config, llm=llm, embed=embed_fn, data_root=agent_dir)
    return SimpleMemBackend(engine)
