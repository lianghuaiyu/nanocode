"""SimpleMemConfig — explicit engine configuration (docs/20 §5.3 / §6.2).

No env reads, no top-level `config.py` import. The host (`MemoryService`)
resolves all values and constructs this dataclass. Two distinct configs never
share mutable state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .retrieval_config import RetrievalConfig


@dataclass(frozen=True)
class SimpleMemConfig:
    # Storage scope (resolved by the host; see vector_store scoping checks).
    root: str
    embed_dimension: int
    table_name: str = "memory_entries"

    # Extraction (write path) knobs.
    window_size: int = 40
    overlap_size: int = 2
    # Resume-extend incremental generation (docs/21 §13.1): how many already-extracted
    # turns immediately preceding the new turns to feed the extractor as prior context
    # (pronoun/antecedent resolution). 0 disables. Context turns are never an extraction
    # target or watermarked; "do-not-extract" is a prompt-level instruction (a restated
    # context fact could still be stored — structural non-extraction needs provenance).
    context_window: int = 6
    enable_parallel_processing: bool = False
    max_parallel_workers: int = 4

    # Fast retrieval (no-LLM hot path) policy — the optimizable action space
    # (docs/22 Phase 1). The host loads the promoted RetrievalConfig from the
    # store root and injects it here; the engine never reads it from disk/env.
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    def __post_init__(self) -> None:
        if self.embed_dimension <= 0:
            raise ValueError("embed_dimension must be a positive int")
        if self.window_size <= 0:
            raise ValueError("window_size must be positive")
        if self.context_window < 0:
            raise ValueError("context_window must be >= 0")
        self.retrieval.validate()
