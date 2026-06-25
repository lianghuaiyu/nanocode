"""Retriever — fast (no-LLM) hybrid + planned (LLM) retrieval (docs/20 §6.5 / docs/22 Phase 1).

`retrieve_fast` is the turn hot path and the `memory search` action. It runs
semantic + lexical + deterministic structured retrieval and merges/dedups via
weighted Reciprocal Rank Fusion — it never calls the LLM. All ranking knobs come
from an injected `RetrievalConfig` (the optimizable action space); the engine
never reads config from disk/env. `retrieve_planned` may call the injected LLM
and is only reachable from explicit planned recall or background maintenance.
"""
from __future__ import annotations

import re

from .llm import LlmClient
from .logging import log
from .models import MemoryEntry
from .retrieval_config import RetrievalConfig
from .vector_store import VectorStore

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "what", "when", "where", "who", "how", "why", "did", "do",
    "does", "with", "at", "by", "from", "this", "that", "it", "i", "you", "we",
})
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TOKEN = re.compile(r"\b[\w]+\b", re.UNICODE)


class Retriever:
    # Reciprocal Rank Fusion constant. RRF fuses ranked lists fairly across
    # channels so an exact lexical/structured rank-1 hit is not buried under the
    # (always-returned) ~25 semantic neighbors — docs/20 §6.5 "真 hybrid".
    RRF_K = 60

    def __init__(self, store: VectorStore, llm: LlmClient,
                 config: "RetrievalConfig | None" = None) -> None:
        self.store = store
        self.llm = llm
        self.config = config or RetrievalConfig()

    def _fuse(self):
        """Return (add, ranked) for weighted Reciprocal Rank Fusion across channels.

        `ranked` is the live entry_id -> [entry, score] map (not truncated) so
        post-fusion boosts can be applied before ordering."""
        ranked: dict[str, list] = {}

        def add(entries: list[MemoryEntry], weight: float) -> None:
            if not weight:
                return
            for rank, e in enumerate(entries):
                contrib = weight / (self.RRF_K + rank + 1)
                slot = ranked.get(e.entry_id)
                if slot is None:
                    ranked[e.entry_id] = [e, contrib]
                else:
                    slot[1] += contrib

        return add, ranked

    # ── fast: no LLM (docs/20 §6.5 / docs/22 Phase 1) ──────────────────
    def retrieve_fast(self, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        if not query or not query.strip():
            return []
        cfg = self.config
        add, ranked = self._fuse()
        for entries, weight in self._fast_channels(query):
            add(entries, weight)
        self._apply_lexical_exact_boost(ranked, query)
        self._apply_time_decay(ranked)
        ordered = sorted(ranked.values(), key=lambda p: p[1], reverse=True)
        cap = max(0, min(limit, cfg.max_context))
        return [p[0] for p in ordered[:cap]]

    def _fast_channels(self, query: str):
        """(entries, weight) per ENABLED channel for the active fusion_mode.

        `rrf` enables all channels; `*_only` modes isolate a single channel so the
        optimizer can probe a pure leg without the others' interference."""
        cfg = self.config
        mode = cfg.fusion_mode
        out: list[tuple[list[MemoryEntry], float]] = []

        if mode in ("rrf", "semantic_only") and cfg.semantic_top_k > 0:
            out.append((self.store.semantic_search(query, top_k=cfg.semantic_top_k),
                        cfg.weight_semantic))

        if mode in ("rrf", "keyword_only") and cfg.keyword_top_k > 0:
            tokens = self._content_tokens(query)
            if tokens:
                out.append((self.store.keyword_search(tokens, top_k=cfg.keyword_top_k),
                            cfg.weight_keyword))

        if mode in ("rrf", "structured_only") and cfg.structured_top_k > 0:
            persons, ts_range = self._deterministic_structured(query)
            if persons:
                # Symbolic person/entity matches are exact (high precision).
                out.append((self.store.structured_search(persons=persons, top_k=cfg.structured_top_k),
                            cfg.weight_structured_person))
                out.append((self.store.structured_search(entities=persons, top_k=cfg.structured_top_k),
                            cfg.weight_structured_entity))
            if ts_range:
                out.append((self.store.structured_search(timestamp_range=ts_range, top_k=cfg.structured_top_k),
                            cfg.weight_timestamp))
        return out

    def _apply_lexical_exact_boost(self, ranked: dict, query: str) -> None:
        """Additive bonus for entries that contain ALL query content tokens
        verbatim (exact lexical match). Default boost 0.0 = no-op (ranking
        identical to plain weighted RRF)."""
        boost = self.config.lexical_exact_boost
        if boost <= 0:
            return
        tokens = self._content_tokens(query)
        if not tokens:
            return
        for slot in ranked.values():
            text = (slot[0].lossless_restatement or "").lower()
            if all(t in text for t in tokens):
                slot[1] += boost

    def _apply_time_decay(self, ranked: dict) -> None:
        """Multiplicative recency decay relative to the newest dated entry in the
        candidate set (clock-free, deterministic). Disabled when half-life is None.

        The engine has no clock (docs/20 §5.3); decay is therefore relative to the
        most recent timestamp present, not wall-clock now — newest entry keeps
        factor 1.0, older entries are downweighted by 0.5**(age_days/half_life)."""
        half_life = self.config.time_decay_half_life_days
        if not half_life:
            return
        dated = [(slot, _parse_epoch_days(slot[0].timestamp)) for slot in ranked.values()]
        days = [d for _slot, d in dated if d is not None]
        if not days:
            return
        newest = max(days)
        for slot, d in dated:
            if d is None:
                continue
            age = newest - d
            if age > 0:
                slot[1] *= 0.5 ** (age / half_life)

    @staticmethod
    def _content_tokens(query: str) -> list:
        toks = [t for t in _TOKEN.findall(query.lower()) if t not in _STOPWORDS and len(t) > 1]
        # dedupe, preserve order
        seen = set()
        out = []
        for t in toks:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    @staticmethod
    def _deterministic_structured(query: str):
        """Deterministic symbolic hints (no LLM): capitalized proper-noun tokens
        as person/entity candidates + ISO dates as a time range."""
        caps = []
        for t in _TOKEN.findall(query):
            if len(t) > 1 and t[:1].isupper() and t.isalpha() and t.lower() not in _STOPWORDS:
                caps.append(t)
        persons = caps[:5] or None
        ts_range = None
        dates = _ISO_DATE.findall(query)
        if dates:
            d = sorted(dates)
            ts_range = (d[0] + "T00:00:00", d[-1] + "T23:59:59")
        return persons, ts_range

    # ── planned: may call LLM (explicit / background only) ────────────
    def retrieve_planned(self, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        """LLM-assisted retrieval. Falls back to fast retrieval on LLM error;
        raises EngineUnavailable only if the LLM is entirely absent."""
        cfg = self.config
        analysis = self._analyze_query(query)
        add, ranked = self._fuse()
        for q in [query] + analysis.get("subqueries", []):
            add(self.store.semantic_search(q, top_k=cfg.semantic_top_k), cfg.weight_semantic)
        kws = analysis.get("keywords") or self._content_tokens(query)
        if kws:
            add(self.store.keyword_search(kws, top_k=cfg.keyword_top_k), cfg.weight_keyword)
        persons = analysis.get("persons") or None
        entities = analysis.get("entities") or None
        if persons or entities:
            add(self.store.structured_search(persons=persons, entities=entities,
                                             top_k=cfg.structured_top_k),
                cfg.weight_structured_person)
        ordered = sorted(ranked.values(), key=lambda p: p[1], reverse=True)
        return [p[0] for p in ordered[:limit]]

    def _analyze_query(self, query: str) -> dict:
        if not self.llm.available:
            return {}
        prompt = (
            "Analyze the query for memory retrieval. Return ONLY JSON with keys: "
            "keywords (list), persons (list), entities (list), subqueries (list of "
            "1-3 alternative phrasings).\n\nQuery: " + query)
        try:
            text = self.llm.complete([
                {"role": "system", "content": "You output only valid JSON."},
                {"role": "user", "content": prompt},
            ])
            data = LlmClient.extract_json(text)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.debug("planned query analysis failed: %s", e)
            return {}


def _parse_epoch_days(ts: "str | None") -> "float | None":
    """ISO timestamp -> fractional days since the UTC epoch (for relative time decay).

    Timezone-safe and host-TZ-independent (docs/22 review): a 'Z' suffix is
    normalized to '+00:00' and a naive datetime is anchored to UTC before
    `.timestamp()`, so the same timestamp yields the same epoch-day value on every
    machine (otherwise `.timestamp()` would read a naive value in the host's local
    zone and break the documented determinism of the no-LLM hot path). Tolerant: a
    missing/unparseable timestamp returns None (no decay applied)."""
    if not ts:
        return None
    from datetime import datetime, timezone
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, OSError):
        m = _ISO_DATE.search(raw)
        if m:
            try:
                dt = datetime.fromisoformat(m.group(1))
            except (ValueError, OSError):
                dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp() / 86400.0
    except (ValueError, OSError, OverflowError):
        return None
