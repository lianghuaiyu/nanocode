"""memory/service.py — host-owned memory boundary (docs/20 §5.1).

`MemoryService` is the single place the host talks to long-term memory. It
owns the backend, the governance policy, the resolved memory paths, and the
host-injected llm/embed callables. It returns packs/hits/tool-strings — it
**never** writes the session tree, emits `ContextInjected`, or touches
tool allowlists (those are `AgentSession` / `CapabilityRouter` jobs).

Boundary invariants (docs/20 §2.1):
- `AgentCore` never imports this module.
- `MemoryService.execute_tool()` is the *only* implementation entry for the
  `memory` tool; the tool module is schema-only.
- Backend errors become user-facing tool results / notices here — backends
  never silently fail or fall back to another backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .models import (
    MemoryHit, MemoryListResult, MemoryReadResult, MemoryWriteResult,
)
from .policy import MemoryPolicy
from .recall import (
    RelevantMemory, MemoryPrefetch, memory_age, memory_freshness_warning,
    MAX_SESSION_MEMORY_BYTES,
)

VALID_BACKENDS = ("off", "markdown", "simplemem")
DEFAULT_BACKEND = "markdown"

# Tool actions exposed in the first-version schema (docs/20 §8).
_HOST_ACTIONS = frozenset({"consolidate"})


@runtime_checkable
class MemoryBackend(Protocol):
    """Explicit read/write capability surface (docs/20 §5.2). No duck typing.

    A backend that cannot perform a write op returns
    `MemoryWriteResult.unsupported_op(...)` — it must never fall back to a
    different backend."""

    name: str

    def retrieve_fast(self, query: str, *, limit: int, token_budget: int) -> list[MemoryHit]:
        """No-LLM ranked retrieval for the turn hot path and tool search."""

    def list(self, *, cursor: "str | None" = None, limit: int = 50) -> MemoryListResult:
        ...

    def read(self, ref: str, *, max_bytes: int) -> MemoryReadResult:
        ...

    def add_note(self, *, title: str, kind: str, content: str, metadata: dict) -> MemoryWriteResult:
        ...

    def update(self, ref: str, *, content: "str | None", metadata: "dict | None") -> MemoryWriteResult:
        ...

    def archive(self, ref: str, *, reason: str) -> MemoryWriteResult:
        ...

    def stats(self) -> dict:
        ...


@dataclass(frozen=True)
class MemoryServiceConfig:
    backend: str                              # "off" | "markdown" | "simplemem"
    use_memories: bool = True
    generate_memories: bool = True
    disable_on_external_context: bool = True
    dedicated_tools: bool = True

    @classmethod
    def resolve(cls, cli_choice: "str | None", *, env: "dict | None" = None) -> "MemoryServiceConfig":
        """Resolve config from CLI choice + env. No "auto"; invalid values fail loud.

        Priority for backend: CLI > NANOCODE_MEMORY_BACKEND > DEFAULT_BACKEND.
        Policy flags read NANOCODE_MEMORY_{USE,GENERATE,EXTERNAL_GUARD}."""
        import os
        env = os.environ if env is None else env
        backend = DEFAULT_BACKEND
        for raw in (cli_choice, env.get("NANOCODE_MEMORY_BACKEND")):
            if raw:
                v = raw.strip().lower()
                if v in VALID_BACKENDS:
                    backend = v
                    break
                raise ValueError(
                    f"unknown memory backend: {raw!r} "
                    f"(valid: {', '.join(VALID_BACKENDS)})")

        def _flag(name: str, default: bool) -> bool:
            raw = env.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            backend=backend,
            use_memories=_flag("NANOCODE_MEMORY_USE", True),
            generate_memories=_flag("NANOCODE_MEMORY_GENERATE", True),
            disable_on_external_context=_flag("NANOCODE_MEMORY_EXTERNAL_GUARD", True),
        )


class MemoryService:
    """Host-owned memory extension boundary (docs/20 §5.1)."""

    def __init__(self, *, config: MemoryServiceConfig, cwd: str, agent_dir: str,
                 llm=None, embed=None, clock=None) -> None:
        self.config = config
        self.cwd = cwd
        self.agent_dir = agent_dir
        self._llm = llm
        self._embed = embed
        self._clock = clock
        self.policy = MemoryPolicy(
            use_memories=config.use_memories,
            generate_memories=config.generate_memories,
            disable_on_external_context=config.disable_on_external_context,
        )
        self._backend = self._build_backend()

    # ── backend construction ─────────────────────────────────────────
    def _build_backend(self) -> MemoryBackend:
        """Build the configured backend. Explicit `simplemem` failure is loud —
        never a silent markdown fallback (docs/20 §2.4)."""
        name = self.config.backend
        if name == "off":
            from .markdown_backend import OffMemoryBackend
            return OffMemoryBackend()
        if name == "markdown":
            from .markdown_backend import MarkdownMemoryBackend
            return MarkdownMemoryBackend()
        if name == "simplemem":
            from .simplemem_backend import create_simplemem_backend
            return create_simplemem_backend(
                cwd=self.cwd, agent_dir=self.agent_dir,
                llm=self._llm, embed=self._embed)
        raise ValueError(f"unknown memory backend: {name!r}")

    @property
    def backend(self) -> MemoryBackend:
        return self._backend

    @property
    def backend_name(self) -> str:
        return self._backend.name

    # ── static guidance (docs/20 §2.2 / Phase 3) ─────────────────────
    def static_prompt(self, request=None) -> str:
        """Backend-aware static memory guidance for the system context.

        Suppressed entirely when memory use is disabled or backend is off."""
        if not self.policy.allows_use or self.backend_name == "off":
            return ""
        from .prompts import build_memory_prompt
        return build_memory_prompt(self._backend)

    # ── turn recall prefetch (docs/20 §2.2 / §6.5: no-LLM hot path) ───
    def start_prefetch(self, query: str, *, already_surfaced: set, session_memory_bytes: int):
        """Start an async no-LLM fast-retrieval prefetch. Returns a
        `MemoryPrefetch` handle (or None when gated/disabled).

        Always uses `retrieve_fast` — the hot path never calls the LLM
        (docs/20 §6.5). The handle's task yields a list of `RelevantMemory`
        ready for injection (dedup against `already_surfaced`)."""
        import asyncio
        import re
        if not self.policy.allows_use or self.backend_name == "off":
            return None
        if not query or not re.search(r"\s", query.strip()):
            return None
        if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
            return None
        surfaced = set(already_surfaced)

        async def _run():
            # Retrieval faults propagate (no silent []): consume_memory_prefetch
            # turns a raised error into an observable Notice (docs/20 §2.4 #5).
            hits = await asyncio.to_thread(
                self._backend.retrieve_fast, query, limit=5, token_budget=0)
            out: list[RelevantMemory] = []
            for h in hits or []:
                rm = self._hit_to_relevant(h)
                if rm.path not in surfaced:
                    out.append(rm)
            return out

        return MemoryPrefetch(asyncio.create_task(_run()))

    def _hit_to_relevant(self, hit: MemoryHit) -> RelevantMemory:
        """Format a `MemoryHit` into the turn-injection currency uniformly."""
        content = hit.content
        mtime_ms = hit.mtime_ms or (self._now_ms())
        freshness = memory_freshness_warning(mtime_ms) if hit.mtime_ms else ""
        ts = f", {hit.timestamp}" if hit.timestamp else ""
        if freshness:
            header = f"{freshness}\n\nMemory: {hit.ref}:"
        elif hit.mtime_ms:
            header = f"Memory (saved {memory_age(mtime_ms)}): {hit.ref}:"
        else:
            header = f"Memory ({self.backend_name}{ts}): {hit.ref}"
        return RelevantMemory(path=hit.ref, content=content, mtime_ms=mtime_ms, header=header)

    def _now_ms(self) -> float:
        if self._clock is not None:
            return self._clock() * 1000
        import time
        return time.time() * 1000

    # ── tool surface (docs/20 §2.3 / §8: the only memory-tool entry) ──
    async def execute_tool(self, inp: dict, *, host) -> str:
        """Sole implementation of the `memory` tool. Returns a user-facing
        string; backend errors become observable results, never silent."""
        if not self.policy.allows_use:
            return "Memory is disabled for this session (use_memories=false)."
        if self.backend_name == "off":
            return "Memory is off for this session (--memory-backend off)."
        action = (inp.get("action") or "").strip()
        if action in _HOST_ACTIONS:
            return await self._execute_host_action(action, inp, host=host)
        if action == "search":
            return self._tool_search(inp)
        if action == "list":
            return self._tool_list(inp)
        if action == "read":
            return self._tool_read(inp)
        if action == "stats":
            return self._tool_stats()
        if action == "add_note":
            return self._tool_add_note(inp)
        return (f"Unknown memory action: {action!r}. "
                f"Valid actions: search, read, list, add_note, stats, consolidate.")

    async def _execute_host_action(self, action: str, inp: dict, *, host) -> str:
        # consolidate spawns a host/session task — never available to sub-agents.
        if getattr(host, "is_sub_agent", False):
            return ("Error: memory consolidation is a host/session operation and "
                    "is not available to sub-agents.")
        # The curator consolidation pipeline is markdown-store specific (it reads
        # the project markdown memory dir). Backend-aware consolidation for the
        # indexed engine is the deferred Phase 6 pipeline — surface that
        # explicitly rather than silently consolidating the wrong store.
        if self.backend_name != "markdown":
            return (f"Memory consolidation is only available for the markdown backend "
                    f"(active backend: {self.backend_name}); backend-aware consolidation "
                    f"is not yet implemented.")
        return await host.spawn_memory_consolidate()

    def _tool_search(self, inp: dict) -> str:
        query = (inp.get("query") or "").strip()
        if not query:
            return "search requires a non-empty 'query'."
        limit = int(inp.get("limit") or 5)
        try:
            hits = self._backend.retrieve_fast(query, limit=limit, token_budget=0)
        except Exception as e:
            return f"[memory] search failed: {e}"
        if not hits:
            return f"No memories matched: {query}"
        out = [f"Top {min(limit, len(hits))} memories for: {query}"]
        for h in hits[:limit]:
            tag = f"[{h.kind}] " if h.kind else ""
            out.append(f"\n{tag}{h.title} ({h.ref})\n{h.content}")
        return "\n".join(out)

    def _tool_list(self, inp: dict) -> str:
        try:
            res = self._backend.list(cursor=inp.get("cursor"), limit=int(inp.get("limit") or 50))
        except Exception as e:
            return f"[memory] list failed: {e}"
        if not res.entries:
            return "No memories saved yet."
        lines = [f"{res.total or len(res.entries)} memories:"]
        for e in res.entries:
            tag = f"[{e.kind}] " if e.kind else ""
            desc = f" — {e.description}" if e.description else ""
            lines.append(f"    {tag}{e.title} ({e.ref}){desc}")
        if res.cursor:
            lines.append(f"\n(more: cursor={res.cursor})")
        return "\n".join(lines)

    def _tool_read(self, inp: dict) -> str:
        ref = (inp.get("ref") or inp.get("filename") or "").strip()
        if not ref:
            return "read requires a 'ref'."
        try:
            res = self._backend.read(ref, max_bytes=int(inp.get("max_bytes") or 8192))
        except Exception as e:
            return f"[memory] read failed: {e}"
        if not res.found:
            return f"Unknown memory: {ref}"
        body = res.content
        if res.truncated:
            body += "\n\n[... truncated ...]"
        return body

    def _tool_stats(self) -> str:
        try:
            s = self.stats()
        except Exception as e:
            return f"[memory] stats failed: {e}"
        return "\n".join(f"{k}: {v}" for k, v in s.items())

    def _tool_add_note(self, inp: dict) -> str:
        title = (inp.get("title") or inp.get("name") or "").strip()
        content = inp.get("content") or ""
        kind = (inp.get("kind") or inp.get("type") or "note").strip()
        if not title or not content:
            return "add_note requires 'title' and 'content'."
        try:
            res = self._backend.add_note(
                title=title, kind=kind, content=content,
                metadata={"description": inp.get("description", ""), "explicit": True})
        except Exception as e:
            return f"[memory] add_note failed: {e}"
        if res.unsupported:
            return f"Unsupported: {res.detail}"
        if not res.ok:
            return f"Failed to add note: {res.detail}"
        # Explicit writes are allowed even on a polluted thread; mark it so the
        # model/user knows this was an explicit write (docs/20 §7 Phase 5).
        return f"Saved memory (explicit): {res.ref}"

    # ── governance hooks (docs/20 §5.1 / Phase 5-6) ──────────────────
    def on_external_context_used(self, *, source: str, thread_id: "str | None" = None) -> bool:
        """Mark the thread polluted when external context is consumed.
        Returns True if the policy mode transitioned."""
        return self.policy.mark_external_context(source)

    # ── generation pipeline (docs/20 §5.1 / §7 Phase 6) ──────────────
    async def maybe_start_generation_pipeline(self, *, thread_id: str, session_mgr,
                                              ephemeral: bool = False,
                                              is_subagent: bool = False,
                                              force: bool = False):
        """Run background memory generation for a completed root session.

        Gated by policy (use/generate/pollution), backend support (simplemem
        only — markdown/off have no extraction engine), and session eligibility
        (root, non-ephemeral, non-sub-agent). Incremental per store: only raw
        branch entries not yet in the store-level watermark are extracted (`force`
        re-extracts the whole branch). Returns a GenerationResult or None when
        generation is not applicable. The extraction runs off-thread; it is
        agent-free and capability-locked by construction (see generate.py).

        `thread_id` identifies the triggering session for diagnostics; the
        generation watermark is keyed on entry ids, not on the session id, so it
        is intentionally not threaded into the pipeline (docs/21 §5)."""
        import asyncio
        from .generate import GenerationEligibility, MemoryGenerationPipeline, GenerationResult
        if not self.policy.allows_generation:
            return GenerationResult.skip("memory generation disabled or thread polluted/disabled")
        engine = getattr(self._backend, "engine", None)
        if self.backend_name != "simplemem" or engine is None:
            return GenerationResult.skip(f"backend {self.backend_name} has no generation engine")
        try:
            turns = self._turns_from_session(session_mgr)
            is_root = session_mgr.spawned_by() is None and session_mgr.forked_from() is None
        except Exception as e:
            return GenerationResult(ran=False, produced=0,
                                    error=f"session read failed: {e}")
        elig = GenerationEligibility(is_root=is_root, is_subagent=is_subagent, ephemeral=ephemeral)
        lease_root = engine.stats().get("root", "")
        # Bounded lease retry (docs/21 §13.2 / D5): when multiple sessions tear down
        # near-simultaneously, the loser waits briefly for the winner instead of
        # dropping its run outright. Still bounded — a lost race stays "not run".
        pipe = MemoryGenerationPipeline(engine, self.policy, lease_timeout=2.0)
        return await asyncio.to_thread(pipe.run, turns, eligibility=elig,
                                       lease_root=lease_root, force=force)

    def _turns_from_session(self, session_mgr) -> "list":
        """Project the canonical raw session branch into `GenerationTurn`s.

        Each turn carries the session-tree entry id so generation can track which
        entries were already extracted (docs/21). MemoryService is host-side and
        may read the session tree to build a transcript; the engine never does.
        Reads the *raw* branch (get_branch) — not build_context(), which folds
        compaction/custom_message summaries into LLM context (docs/21 §12.1)."""
        from ..session import tree as _tree
        from .generate import GenerationTurn
        turns = []
        branch = session_mgr.get_branch()
        for e in branch:
            if getattr(e, "type", None) != _tree.MESSAGE:
                continue
            msg = (e.data or {}).get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = self._message_text(msg.get("content"))
            if text.strip():
                turns.append(GenerationTurn(
                    entry_id=e.id,
                    speaker=role,
                    content=text,
                    timestamp=getattr(e, "timestamp", None),
                ))
        return turns

    @staticmethod
    def _message_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif isinstance(b, str):
                    parts.append(b)
            return "\n".join(parts)
        return ""

    # ── stats ─────────────────────────────────────────────────────────
    def stats(self) -> dict:
        s = dict(self._backend.stats())
        s.setdefault("backend", self.backend_name)
        s["use_memories"] = self.policy.use_memories
        s["generate_memories"] = self.policy.generate_memories
        s["mode"] = self.policy.mode
        return s
