"""memory/generate.py — background memory generation pipeline (docs/20 §7 Phase 6).

First version, deliberately simplified but with the same hard boundaries as
Codex's pipeline:

- Only root, non-ephemeral, non-sub-agent, non-polluted/disabled sessions are
  eligible (gates below + `MemoryPolicy.allows_generation`).
- The worker is **capability-locked by construction**: it performs a direct,
  no-agent extraction call into the engine (`engine.add_dialogues`). It never
  spawns a sub-agent, never exposes the memory tool, never touches MCP /
  plugins / network beyond the host-injected extraction LLM — so recursive
  memory writes are impossible, not merely disallowed.
- A `MemoryJobLease` serializes workers; a worker failure leaves the existing
  index untouched (no delete/rebuild on this path).

Progress model (docs/21): generation tracks *which raw session entries* have
already been extracted, keyed by stable session-tree entry id — not a per-session
boolean. The watermark is a single **store-level** set of extracted entry ids, so
resume-extend, branch switches, and id-preserving `clone`/`fork` all dedup against
the same set (entry ids are globally stable, see session/tree.py). On resume the
worker only extracts entries whose ids are not yet in the set.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .jobs import MemoryJobLease

_STATE_SCHEMA = 1


@dataclass(frozen=True)
class GenerationTurn:
    """One raw user/assistant message entry, projected for extraction.

    `entry_id` is the session-tree entry id — the stable fact the watermark is
    keyed on (docs/21 §12.2: never round-count, never list index)."""
    entry_id: str
    speaker: str
    content: str
    timestamp: "str | None" = None


def _state_dir(lease_root: str) -> Path:
    return Path(lease_root) / ".generated_entries"


def _state_path(lease_root: str) -> Path:
    # Store-level (not per-session): one set of extracted entry ids per engine
    # store, so clone/fork (which copy entries verbatim, preserving ids) and
    # branch switches never re-extract already-seen entries (docs/21 §5/§12.2).
    return _state_dir(lease_root) / "state.json"


def read_extracted_entry_ids(lease_root: str) -> set:
    """Set of entry ids already extracted into this store. Missing file → empty.

    Malformed/unknown-schema state raises (no silent empty-set): the caller turns
    it into an observable GenerationResult error rather than silently re-extracting
    the whole transcript (docs/21 §6 rule 4)."""
    path = _state_path(lease_root)
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != _STATE_SCHEMA:
        raise ValueError(f"unsupported generated state schema: {data.get('schema')!r}")
    ids = data.get("extracted_entry_ids")
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise ValueError("generated state extracted_entry_ids must be a list[str]")
    return set(ids)


def write_extracted_entry_ids(lease_root: str, ids: set) -> None:
    """Atomically persist the extracted-id set. Sorted for stable diffs/tests.

    Atomic via same-dir temp + os.replace so a crash mid-write never leaves a
    half-written state file (which read_extracted_entry_ids would then raise on)."""
    path = _state_path(lease_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")   # append, never with_suffix (a dotted id would mangle the stem)
    tmp.write_text(
        json.dumps(
            {"schema": _STATE_SCHEMA, "extracted_entry_ids": sorted(ids)},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


@dataclass(frozen=True)
class GenerationEligibility:
    is_root: bool
    is_subagent: bool
    ephemeral: bool

    def check(self) -> "tuple[bool, str]":
        if self.is_subagent:
            return False, "sub-agent session"
        if self.ephemeral:
            return False, "ephemeral session"
        if not self.is_root:
            return False, "non-root session"
        return True, ""


@dataclass
class GenerationResult:
    ran: bool
    produced: int = 0
    skipped_reason: "str | None" = None
    error: "str | None" = None

    @classmethod
    def skip(cls, reason: str) -> "GenerationResult":
        return cls(ran=False, skipped_reason=reason)


class MemoryGenerationPipeline:
    """Extract memory units from a completed root session into the engine."""

    def __init__(self, engine, policy, *, lease_timeout: float = 0.0) -> None:
        self._engine = engine
        self._policy = policy
        self._lease_timeout = lease_timeout

    def run(self, turns: "list[GenerationTurn]", *, eligibility: GenerationEligibility,
            lease_root: str, force: bool = False) -> GenerationResult:
        """turns: the raw user/assistant entries of the current branch as
        `GenerationTurn`s. Pure, synchronous.

        Incremental per store: only entries whose ids are not already in the
        store-level watermark are extracted, so resume-then-continue extracts just
        the new turns. `force=True` re-extracts the whole branch (accepting
        duplicate index entries) but still only *unions* the branch ids into the
        watermark — it never clears ids from other branches (docs/21 §5)."""
        if not self._policy.allows_generation:
            return GenerationResult.skip("memory generation disabled or thread polluted/disabled")
        ok, reason = eligibility.check()
        if not ok:
            return GenerationResult.skip(reason)
        if not turns:
            return GenerationResult.skip("no eligible turns to extract")
        lease = MemoryJobLease.acquire(lease_root, timeout=self._lease_timeout)
        if lease is None:
            # Best-effort lease: a lost race is "not run", never "generated".
            # The watermark is untouched, so a later trigger re-extracts these
            # turns (docs/21 §5: contention is recoverable, not silent loss).
            return GenerationResult.skip("another generation worker holds the lease")
        try:
            # State read under the lease so the watermark is race-free. Malformed
            # state is observable and gates the engine call — never re-extract on a
            # corrupt watermark (docs/21 §6 rule 4).
            try:
                existing = read_extracted_entry_ids(lease_root)
            except Exception as e:
                return GenerationResult(ran=False, produced=0, error=str(e))

            new_turns = list(turns) if force else [t for t in turns if t.entry_id not in existing]
            if not new_turns:
                return GenerationResult.skip("no new turns since last generation")

            from .engines.simplemem import Dialogue

            def _dlg(seq, t):
                return Dialogue(dialogue_id=seq, speaker=t.speaker,
                                content=t.content, timestamp=t.timestamp)

            dialogues = [_dlg(i + 1, t) for i, t in enumerate(new_turns)]
            # Read-only context (docs/21 §13.1): the already-extracted turns immediately
            # preceding the first new turn, for pronoun/antecedent resolution. The engine
            # truncates to its context_window and prompts the model not to extract them;
            # they are never an extraction target and never watermarked (prompt-level — a
            # restated context fact could still be stored; structural non-extraction needs
            # provenance, schema v2). force re-extracts the whole branch, so first_new_idx
            # == 0 → no separate context.
            new_ids = {t.entry_id for t in new_turns}
            first_new_idx = next((i for i, t in enumerate(turns) if t.entry_id in new_ids), 0)
            context_dialogues = [_dlg(i + 1, t) for i, t in enumerate(turns[:first_new_idx])]
            try:
                produced = self._engine.add_dialogues(dialogues, context_dialogues=context_dialogues)
            except Exception as e:
                # A worker failure must not advance extraction progress. The
                # engine write path commits at the job boundary; if anything in
                # extraction/write fails, the watermark is NOT advanced and a
                # later run retries this batch.
                # (Extraction is fail-loud: a legal empty `[]` returns normally
                # below and *does* advance the watermark — docs/21 §6 rule 4.)
                return GenerationResult(ran=True, produced=0, error=str(e))

            # Union, never replace: force keeps ids from other branches, and the
            # filtered path keeps everything already extracted.
            next_ids = existing | new_ids
            write_extracted_entry_ids(lease_root, next_ids)
            return GenerationResult(ran=True, produced=len(produced))
        finally:
            lease.release()
