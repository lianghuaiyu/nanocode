"""LanceDB-backed multi-view vector store (forked, docs/20 §6.6).

Changes from upstream: no global `config`/env, scoped+validated root, schema
version marker (MigrationRequired on mismatch), structured logging instead of
`print`, dataclass `MemoryEntry`, host-injected `Embedder`.
"""
from __future__ import annotations

from pathlib import Path

from .embeddings import Embedder
from .logging import log
from .migrations import ensure_schema
from .models import MemoryEntry


class VectorStore:
    def __init__(self, root: Path, embedder: Embedder, *, table_name: str = "memory_entries") -> None:
        import lancedb

        self.root = root
        self.embedder = embedder
        self.table_name = table_name
        self._fts_initialized = False
        self._fts_available = True
        root.mkdir(parents=True, exist_ok=True)
        ensure_schema(root)
        self.db = lancedb.connect(str(root))
        self._init_table()

    def _table_names(self) -> list:
        fn = getattr(self.db, "list_tables", None)
        if callable(fn):
            return list(fn())
        return list(self.db.table_names())

    def _init_table(self) -> None:
        import pyarrow as pa
        schema = pa.schema([
            pa.field("entry_id", pa.string()),
            pa.field("lossless_restatement", pa.string()),
            pa.field("keywords", pa.list_(pa.string())),
            pa.field("timestamp", pa.string()),
            pa.field("location", pa.string()),
            pa.field("persons", pa.list_(pa.string())),
            pa.field("entities", pa.list_(pa.string())),
            pa.field("topic", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), self.embedder.dimension)),
        ])
        if self.table_name not in self._table_names():
            self.table = self.db.create_table(self.table_name, schema=schema)
            log.debug("created table %s", self.table_name)
        else:
            self.table = self.db.open_table(self.table_name)
            log.debug("opened table %s", self.table_name)

    def _init_fts_index(self) -> None:
        if self._fts_initialized:
            return
        # Native FTS (lancedb >= 0.30 removed the tantivy backend). The lexical
        # leg is best-effort: if the index can't be built we degrade to
        # semantic+structured but record an OBSERVABLE warning + health flag so
        # a dead lexical leg is never silent (docs/20 §2.4/§6.5).
        try:
            self.table.create_fts_index("lossless_restatement", replace=True)
            self._fts_initialized = True
            self._fts_available = True
            log.debug("native FTS index created")
        except Exception as e:
            self._fts_available = False
            log.warning("FTS index unavailable; lexical retrieval degraded: %s", e)

    def _to_entries(self, rows: list) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        for r in rows:
            try:
                out.append(MemoryEntry(
                    entry_id=r["entry_id"],
                    lossless_restatement=r["lossless_restatement"],
                    keywords=list(r.get("keywords") or []),
                    timestamp=r.get("timestamp") or None,
                    location=r.get("location") or None,
                    persons=list(r.get("persons") or []),
                    entities=list(r.get("entities") or []),
                    topic=r.get("topic") or None,
                ))
            except Exception as e:
                log.debug("skipping unparseable row: %s", e)
        return out

    def add_entries(self, entries: list[MemoryEntry]) -> None:
        if not entries:
            return
        vectors = self.embedder.encode_documents([e.lossless_restatement for e in entries])
        data = [{
            "entry_id": e.entry_id,
            "lossless_restatement": e.lossless_restatement,
            "keywords": e.keywords,
            "timestamp": e.timestamp or "",
            "location": e.location or "",
            "persons": e.persons,
            "entities": e.entities,
            "topic": e.topic or "",
            "vector": vec,
        } for e, vec in zip(entries, vectors)]
        self.table.add(data)
        log.debug("added %d entries", len(entries))
        if not self._fts_initialized:
            self._init_fts_index()

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    def count(self) -> int:
        # Load-bearing: a corrupt/unreadable table must surface, not read as
        # "empty" (docs/20 §2.4 #5 — no silent []).
        return int(self.table.count_rows())

    def semantic_search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        # Load-bearing leg: real faults (embed endpoint down, table read error)
        # propagate so the service surfaces an observable error. An empty table
        # is a legitimate empty result, not a fault.
        if self.count() == 0:
            return []
        qv = self.embedder.encode_query(query)
        rows = self.table.search(qv).limit(top_k).to_list()
        return self._to_entries(rows)

    def keyword_search(self, keywords: list, top_k: int = 5) -> list[MemoryEntry]:
        if not keywords or self.count() == 0:
            return []
        if not self._fts_initialized:
            self._init_fts_index()
        if not self._fts_available:
            return []
        # Lexical leg is best-effort: a query-time FTS error degrades to empty
        # (other legs carry the hybrid) but is logged at warning, not hidden.
        try:
            rows = self.table.search(" ".join(keywords), query_type="fts").limit(top_k).to_list()
            return self._to_entries(rows)
        except Exception as e:
            log.warning("lexical (FTS) search failed; degrading: %s", e)
            return []

    def structured_search(self, *, persons=None, entities=None, location=None,
                          timestamp_range=None, top_k: int = 5) -> list[MemoryEntry]:
        if self.count() == 0 or not any([persons, entities, location, timestamp_range]):
            return []
        conditions = []
        if persons:
            vals = ", ".join("'" + p.replace("'", "''") + "'" for p in persons)
            conditions.append(f"array_has_any(persons, make_array({vals}))")
        if entities:
            vals = ", ".join("'" + e.replace("'", "''") + "'" for e in entities)
            conditions.append(f"array_has_any(entities, make_array({vals}))")
        if location:
            safe = location.replace("'", "''")
            conditions.append(f"location LIKE '%{safe}%'")
        if timestamp_range:
            start, end = timestamp_range
            conditions.append(f"timestamp >= '{start}' AND timestamp <= '{end}'")
        # Symbolic leg is a best-effort hint: degrade to empty on filter error.
        try:
            q = self.table.search().where(" AND ".join(conditions), prefilter=True).limit(top_k)
            return self._to_entries(q.to_list())
        except Exception as e:
            log.warning("structured search failed; degrading: %s", e)
            return []

    def get_all_entries(self) -> list[MemoryEntry]:
        # Load-bearing (list/read/stats): faults propagate, never silent [].
        return self._to_entries(self.table.to_arrow().to_pylist())

