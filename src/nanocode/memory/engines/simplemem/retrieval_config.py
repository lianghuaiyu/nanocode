"""RetrievalConfig — the no-LLM retrieval action space (docs/22 §2.4 / Phase 1).

The optimizable surface of `retrieve_fast`. First version only: fields that
change *retrieval* behaviour with no LLM call, no answer-generation policy, no
benchmark-adapter flags (those would pollute the turn hot path — docs/22 §2.4).

A frozen dataclass with fail-loud (de)serialization:
- `from_dict` rejects unknown fields (no silent ignore) — a typo in a persisted
  config is observable, not silently dropped.
- `to_dict` is stably ordered for diffs / history.
- `validate` enforces ranges (fail loud).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

FUSION_MODES = ("rrf", "semantic_only", "keyword_only", "structured_only")

# Finite upper bounds for fail-loud validation of a persisted/hand-edited config.
# Generous (not the optimizer's tight search bounds) — just enough to keep an
# absurd value out of the no-LLM hot path.
_MAX_TOP_K = 1000
_MAX_CONTEXT = 1000
_MAX_WEIGHT = 1000.0
_MAX_HALF_LIFE_DAYS = 100000.0


@dataclass(frozen=True)
class RetrievalConfig:
    schema_version: int = 1
    semantic_top_k: int = 25
    keyword_top_k: int = 5
    structured_top_k: int = 5
    max_context: int = 5
    fusion_mode: str = "rrf"
    weight_semantic: float = 1.0
    weight_keyword: float = 1.0
    weight_structured_person: float = 1.0
    weight_structured_entity: float = 1.0
    weight_timestamp: float = 0.6
    lexical_exact_boost: float = 0.0
    time_decay_half_life_days: "float | None" = None

    def __post_init__(self) -> None:
        self.validate()

    # ── validation (fail loud) ────────────────────────────────────────
    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported RetrievalConfig schema_version: {self.schema_version!r}")
        if self.fusion_mode not in FUSION_MODES:
            raise ValueError(f"fusion_mode must be one of {FUSION_MODES}, got {self.fusion_mode!r}")
        # bool is an int subclass — reject it explicitly so a JSON true/false in a
        # persisted config is observable, not silently coerced to 0/1. Finite upper
        # bounds keep a corrupt/hand-edited config from pushing absurd values onto
        # the no-LLM hot path (e.g. a giant top_k into vector_store .limit()).
        for name, hi in (("semantic_top_k", _MAX_TOP_K), ("keyword_top_k", _MAX_TOP_K),
                         ("structured_top_k", _MAX_TOP_K)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > hi:
                raise ValueError(f"{name} must be an int in [0, {hi}], got {v!r}")
        if (isinstance(self.max_context, bool) or not isinstance(self.max_context, int)
                or self.max_context < 1 or self.max_context > _MAX_CONTEXT):
            raise ValueError(f"max_context must be an int in [1, {_MAX_CONTEXT}], got {self.max_context!r}")
        for name in ("weight_semantic", "weight_keyword", "weight_structured_person",
                     "weight_structured_entity", "weight_timestamp", "lexical_exact_boost"):
            v = getattr(self, name)
            if (isinstance(v, bool) or not isinstance(v, (int, float))
                    or not math.isfinite(v) or v < 0 or v > _MAX_WEIGHT):
                raise ValueError(f"{name} must be a finite number in [0, {_MAX_WEIGHT}], got {v!r}")
        if self.time_decay_half_life_days is not None:
            v = self.time_decay_half_life_days
            if (isinstance(v, bool) or not isinstance(v, (int, float))
                    or not math.isfinite(v) or v <= 0 or v > _MAX_HALF_LIFE_DAYS):
                raise ValueError(
                    f"time_decay_half_life_days must be a finite number in (0, {_MAX_HALF_LIFE_DAYS}] "
                    f"or None, got {v!r}")

    # ── (de)serialization ─────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict) -> "RetrievalConfig":
        if not isinstance(data, dict):
            raise ValueError("RetrievalConfig data must be a dict")
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown RetrievalConfig field(s): {sorted(unknown)}")
        return cls(**data)

    def to_dict(self) -> dict:
        # asdict preserves field declaration order; that order is stable for diffs.
        return asdict(self)
