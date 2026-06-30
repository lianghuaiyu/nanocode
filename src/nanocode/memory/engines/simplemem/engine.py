"""SimpleMemEngine — public engine API (docs/20 §5.3).

A pure memory algorithm/index layer. It does not import runtime/session/
capabilities, never constructs network clients, never reads env, and never
prints. LLM/embedding are injected by the host.
"""
from __future__ import annotations

from .config import SimpleMemConfig
from .embeddings import Embedder
from .llm import LlmClient
from .memory_builder import MemoryBuilder
from .migrations import resolve_scoped_root
from .models import Dialogue, MemoryEntry, MemoryNote, MemoryPage
from .retriever import Retriever
from .vector_store import VectorStore


class SimpleMemEngine:
    def __init__(self, config: SimpleMemConfig, *, llm: LlmClient, embedder: Embedder,
                 store: VectorStore) -> None:
        self.config = config
        self._llm = llm
        self._embedder = embedder
        self._store = store
        self._retriever = Retriever(store, llm, config.retrieval)

    # ── write ─────────────────────────────────────────────────────────
    def add_dialogues(self, dialogues: list[Dialogue], *,
                      context_dialogues: "list[Dialogue] | tuple" = ()) -> list[MemoryEntry]:
        """LLM-extracted write path (generation/consolidation only).

        Fails loud when no LLM callable is injected — generation never silently
        produces nothing (docs/20 §2.4).

        A fresh `MemoryBuilder` is constructed per call so its sliding-window
        `_previous` dedup context only spans *this* job's dialogues. A long-lived
        builder would carry the last job's extracted restatements into the next
        job's prompt — wrong cross-session/cross-job context, especially under
        the resume-extend incremental path where each call sees only new turns
        (docs/21 §7.3).

        `context_dialogues` are prior turns supplied for pronoun/antecedent
        resolution (docs/21 §13.1). The engine keeps only the last `context_window`
        of them and surfaces them in the extraction prompt under a do-NOT-extract
        header. They are never an extraction *target* and never watermarked — but
        "context-only" is a PROMPT-LEVEL instruction (docs/21 §11), not a structural
        guarantee: a model that restates a context fact would still store it.
        Structural non-extraction needs source provenance (deferred to schema v2,
        docs/21 §14.4)."""
        if not self._llm.available:
            from .errors import EngineUnavailable
            raise EngineUnavailable(
                "SimpleMem dialogue extraction requires an injected llm callable")
        cw = self.config.context_window
        context = list(context_dialogues)[-cw:] if cw > 0 else []
        builder = MemoryBuilder(self._llm, window_size=self.config.window_size,
                                overlap_size=self.config.overlap_size,
                                context_dialogues=context)
        produced = builder.add_dialogues(dialogues)
        produced += builder.finalize()
        if produced:
            # Commit only after every extraction window succeeds. This keeps the
            # generation watermark and index atomic at the job boundary.
            self._store.add_entries(produced)
        return produced

    def add_note(self, note: MemoryNote) -> MemoryEntry:
        """Direct, no-LLM write of an explicit note."""
        entry = MemoryEntry(
            lossless_restatement=note.content,
            keywords=note.keywords or _keywords_from_title(note.title),
            timestamp=note.timestamp,
            topic=note.title,
        )
        self._store.add_entries([entry])
        return entry

    # ── read ──────────────────────────────────────────────────────────
    def retrieve_fast(self, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        """No LLM. semantic + lexical + deterministic structured heuristics."""
        return self._retriever.retrieve_fast(query, limit=limit)

    def retrieve_with_config(self, query: str, config, *, limit: int = 5) -> list[MemoryEntry]:
        """No-LLM retrieval against a *candidate* RetrievalConfig without mutating
        the live retriever (docs/22 §架构决策 3). The host optimizer uses this to
        score candidate policies on the same store before promotion."""
        from .retriever import Retriever
        return Retriever(self._store, self._llm, config).retrieve_fast(query, limit=limit)

    def retrieve_planned(self, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        """May call the host LLM (explicit/background recall only)."""
        return self._retriever.retrieve_planned(query, limit=limit)

    def list_entries(self, *, limit: int = 50, cursor: "str | None" = None) -> MemoryPage:
        entries = self._store.get_all_entries()
        return MemoryPage(entries=entries[:limit], cursor=None, total=len(entries))

    def read_entry(self, entry_id: str) -> "MemoryEntry | None":
        for e in self._store.get_all_entries():
            if e.entry_id == entry_id:
                return e
        return None

    def stats(self) -> dict:
        return {
            "backend": "simplemem",
            "count": self._store.count(),
            "root": str(self._store.root),
            "embed_available": self._embedder.available,
            "llm_available": self._llm.available,
            "fts_available": self._store.fts_available,
        }


def _keywords_from_title(title: str) -> list:
    return [w for w in title.replace("/", " ").split() if len(w) > 1][:8]


def create_simplemem_engine(config: SimpleMemConfig, *, llm=None, embed=None,
                            data_root: str) -> SimpleMemEngine:
    """Construct an engine with a validated, data-root-scoped store.

    `llm` is a callable(messages)->str or None; `embed` is a
    callable(texts)->list[list[float]] or None. Embedding is required to build
    the store; an absent embed callable fails loud here."""
    root = resolve_scoped_root(config.root, data_root=data_root)
    embedder = Embedder(embed, config.embed_dimension)
    if not embedder.available:
        from .errors import EngineUnavailable
        raise EngineUnavailable(
            "SimpleMem requires an embedding callable to build its index")
    llm_client = LlmClient(llm)
    store = VectorStore(root, embedder, table_name=config.table_name)
    return SimpleMemEngine(config, llm=llm_client, embedder=embedder, store=store)
