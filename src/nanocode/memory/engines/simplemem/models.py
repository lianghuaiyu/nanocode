"""SimpleMem engine models (docs/20 §6: plain dataclasses, no pydantic).

The upstream models used pydantic; the fork uses stdlib dataclasses to drop a
dependency and keep the engine a pure algorithm layer.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class MemoryEntry:
    """A compact, self-contained memory unit with multi-view index fields.

    - Semantic layer: `lossless_restatement` (dense-embedded)
    - Lexical layer: `keywords` (FTS / exact match)
    - Symbolic layer: timestamp/location/persons/entities/topic (metadata)
    """

    lossless_restatement: str
    entry_id: str = field(default_factory=_new_id)
    keywords: list[str] = field(default_factory=list)
    timestamp: "str | None" = None
    location: "str | None" = None
    persons: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    topic: "str | None" = None


@dataclass
class Dialogue:
    """An original dialogue line fed to the extraction pipeline."""

    dialogue_id: int
    speaker: str
    content: str
    timestamp: "str | None" = None

    def __str__(self) -> str:
        ts = f"[{self.timestamp}] " if self.timestamp else ""
        return f"{ts}{self.speaker}: {self.content}"


@dataclass
class MemoryNote:
    """An explicit, host-confirmed note written directly (no LLM extraction)."""

    title: str
    content: str
    kind: str = "note"
    keywords: list[str] = field(default_factory=list)
    timestamp: "str | None" = None
    metadata: dict = field(default_factory=dict)


@dataclass
class MemoryPage:
    """A page of entries for listing."""

    entries: list[MemoryEntry] = field(default_factory=list)
    cursor: "str | None" = None
    total: int = 0
