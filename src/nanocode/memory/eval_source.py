"""memory/eval_source.py — backend-aware EVAL-mode curator input (docs/22 Phase 2).

The EVAL-mode curator proposes QA candidates from the user's stored memories.
The input must reflect the *active* backend, not always markdown files:

- markdown  → the markdown memory files (legacy semantics).
- simplemem → indexed entries, each labelled with its `simplemem://<entry_id>`
  ref so the curator can cite a stable memory_ref.
- off       → a sentinel so the host short-circuits without spawning a task.

The curator never sees or touches the store path; it only sees formatted text.
"""
from __future__ import annotations

import time

_NO_MEMORIES = "No memories found. Return an empty candidate list."


def build_eval_curator_message(backend, *, max_entries: int = 300,
                               max_bytes: int = 200_000) -> str:
    """Backend-aware curator input. `backend` is a `MemoryBackend` (or None)."""
    name = getattr(backend, "name", "off")
    if backend is None or name == "off":
        return _NO_MEMORIES
    if name == "markdown":
        return _markdown_message()
    if name == "simplemem":
        return _simplemem_message(backend, max_entries=max_entries, max_bytes=max_bytes)
    return _NO_MEMORIES


def valid_memory_refs(backend) -> "set[str]":
    """The set of currently-existing memory refs for the active backend.

    Used by eval pruning to drop candidates whose source memory no longer exists
    (backend-aware — docs/22 §2 Phase 2). markdown → filenames; simplemem →
    `simplemem://<entry_id>` refs."""
    name = getattr(backend, "name", "off")
    if name == "markdown":
        from ..paths import project_memory_dir
        return {f.name for f in project_memory_dir().glob("*.md") if f.name != "MEMORY.md"}
    if name == "simplemem":
        engine = getattr(backend, "engine", None)
        if engine is None:
            return set()
        page = engine.list_entries(limit=100000)
        return {f"simplemem://{e.entry_id}" for e in page.entries}
    return set()


def _markdown_message() -> str:
    from ..paths import project_memory_dir
    mem_dir = project_memory_dir()
    parts = [f"Today's date: {time.strftime('%Y-%m-%d')}", ""]
    parts.append("# Memory entries (derive QA eval candidates from these)\n")
    files = sorted(mem_dir.glob("*.md"))
    wrote = False
    for f in files:
        if f.name == "MEMORY.md":
            continue
        try:
            content = f.read_text()
        except OSError:
            continue
        parts.append(f"## Memory: {f.name}\n\n{content}\n")
        wrote = True
    if not wrote:
        return _NO_MEMORIES
    return "\n".join(parts)


def _simplemem_message(backend, *, max_entries: int, max_bytes: int) -> str:
    engine = getattr(backend, "engine", None)
    if engine is None:
        return _NO_MEMORIES
    page = engine.list_entries(limit=max_entries)
    if not page.entries:
        return _NO_MEMORIES
    parts = [f"Today's date: {time.strftime('%Y-%m-%d')}", ""]
    parts.append("# Memory entries (derive QA eval candidates from these)\n")
    size = sum(len(p) for p in parts)
    for e in page.entries:
        block = [f"## Memory: simplemem://{e.entry_id}", e.lossless_restatement]
        if e.keywords:
            block.append("keywords: " + ", ".join(e.keywords))
        if e.timestamp:
            block.append("time: " + e.timestamp)
        if e.persons:
            block.append("persons: " + ", ".join(e.persons))
        chunk = "\n".join(block) + "\n"
        if size + len(chunk) > max_bytes:
            break
        parts.append(chunk)
        size += len(chunk)
    return "\n".join(parts)
