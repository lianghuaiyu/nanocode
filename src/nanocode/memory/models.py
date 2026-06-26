"""memory/models.py — host-facing memory value objects (docs/20 §5.2).

These are the typed currency between `MemoryService`, the `MemoryBackend`
protocol, and the memory tool. They are deliberately backend-agnostic: a
markdown file and a SimpleMem index entry both project onto the same shapes,
so the host boundary never leaks engine internals.

`RelevantMemory` (the turn-recall injection currency) lives in `recall.py` and
is intentionally separate — it carries injection headers/freshness, while
`MemoryHit` carries ranked retrieval data for the tool surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MemoryHit:
    """A single ranked memory unit returned by fast retrieval / tool search."""

    ref: str                       # stable reference: markdown filename or simplemem://<id>
    title: str
    content: str
    kind: str = ""                 # user|feedback|project|reference|note|""
    score: float = 0.0
    timestamp: "str | None" = None
    mtime_ms: float = 0.0


@dataclass(frozen=True)
class MemoryEntryView:
    """A lightweight memory descriptor for listing (no full body)."""

    ref: str
    title: str
    kind: str = ""
    description: str = ""


@dataclass(frozen=True)
class MemoryListResult:
    entries: list[MemoryEntryView] = field(default_factory=list)
    cursor: "str | None" = None
    total: int = 0


@dataclass(frozen=True)
class MemoryReadResult:
    ref: str
    content: str = ""
    found: bool = True
    truncated: bool = False


@dataclass(frozen=True)
class MemoryWriteResult:
    """Result of a write-shaped backend op (add_note/update/archive).

    `unsupported=True` is how a backend declines a capability *explicitly* —
    the service turns it into a user-facing "unsupported" message and never
    silently falls back to another backend (docs/20 §2.4 / §5.2).
    """

    ref: "str | None"
    ok: bool
    detail: str = ""
    unsupported: bool = False

    @classmethod
    def unsupported_op(cls, op: str, backend: str) -> "MemoryWriteResult":
        return cls(ref=None, ok=False, unsupported=True,
                   detail=f"{op} is not supported by the {backend} memory backend")
