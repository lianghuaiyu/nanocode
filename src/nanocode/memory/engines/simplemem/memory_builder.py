"""MemoryBuilder — LLM extraction path (forked, docs/20 §6).

Sliding-window extraction of compact memory units from dialogues, using the
host-injected LLM. No global config, no print. Extraction requires the LLM;
direct note writes (engine.add_note) bypass this entirely. The builder does not
write storage; the engine commits the full job only after every window succeeds.
"""
from __future__ import annotations

from .llm import LlmClient
from .logging import log
from .models import Dialogue, MemoryEntry

_SYSTEM = ("You are a professional information extraction assistant. You extract "
           "structured, unambiguous memory units and output only valid JSON.")


class MemoryBuilder:
    def __init__(self, llm: LlmClient, *, window_size: int = 40, overlap_size: int = 2,
                 context_dialogues: "list[Dialogue] | tuple" = ()) -> None:
        self.llm = llm
        self.window_size = max(1, window_size)
        self.overlap_size = max(0, overlap_size)
        self.step_size = max(1, self.window_size - self.overlap_size)
        self._buffer: list[Dialogue] = []
        self._previous: list[MemoryEntry] = []
        # Prior turns for pronoun/antecedent resolution (docs/21 §13.1): surfaced in
        # the prompt under a do-NOT-extract header. This is a prompt-level instruction,
        # not a structural guarantee (structural non-extraction needs provenance).
        self._context_dialogues = list(context_dialogues)

    def add_dialogues(self, dialogues: list[Dialogue]) -> list[MemoryEntry]:
        self._buffer.extend(dialogues)
        produced: list[MemoryEntry] = []
        while len(self._buffer) >= self.window_size:
            produced.extend(self._process_window())
        return produced

    def finalize(self) -> list[MemoryEntry]:
        if not self._buffer:
            return []
        window = self._buffer
        self._buffer = []
        return self._emit(self._generate_entries(window))

    def _process_window(self) -> list[MemoryEntry]:
        window = self._buffer[:self.window_size]
        self._buffer = self._buffer[self.step_size:]
        return self._emit(self._generate_entries(window))

    def _emit(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        if entries:
            self._previous = entries
        return entries

    def _generate_entries(self, dialogues: list[Dialogue]) -> list[MemoryEntry]:
        prompt = self._extraction_prompt(dialogues)
        last_error: "Exception | None" = None
        for attempt in range(3):
            try:
                text = self.llm.complete([
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ])
                data = LlmClient.extract_json(text)
                if not isinstance(data, list):
                    raise ValueError(f"expected JSON array, got {type(data).__name__}")
                # A legal empty array is a *success* (nothing worth extracting).
                # But a non-empty array whose items are malformed (non-dict, or
                # missing a required field via _to_entry) is a FAILURE, not a set
                # of silently dropped entries: e.g. `[null]` must NOT masquerade
                # as "nothing to remember", or the generation watermark would
                # advance past a batch that was never actually extracted
                # (docs/21 fail-loud contract; caught by codex review).
                entries = []
                for item in data:
                    if not isinstance(item, dict):
                        raise ValueError(f"extraction item is not a JSON object: {item!r}")
                    entries.append(self._to_entry(item))
                return entries
            except Exception as e:
                last_error = e
        # Exhausted retries: fail loud. Returning [] here would let the caller's
        # watermark treat a failed extraction as "nothing to remember" and never
        # retry this batch (docs/21 §12.3) — the ExtractionFailed/[] split is the
        # whole contract that makes write-on-empty safe.
        from .errors import ExtractionFailed
        log.warning("extraction failed after 3 attempts: %s", last_error)
        raise ExtractionFailed(f"memory extraction failed: {last_error}") from last_error

    @staticmethod
    def _to_entry(item: dict) -> MemoryEntry:
        return MemoryEntry(
            lossless_restatement=item["lossless_restatement"],
            keywords=list(item.get("keywords") or []),
            timestamp=item.get("timestamp") or None,
            location=item.get("location") or None,
            persons=list(item.get("persons") or []),
            entities=list(item.get("entities") or []),
            topic=item.get("topic") or None,
        )

    def _extraction_prompt(self, dialogues: list[Dialogue]) -> str:
        text = "\n".join(str(d) for d in dialogues)
        ctx = ""
        if self._previous:
            ctx = "\n[Previously extracted (avoid duplication)]\n" + "\n".join(
                f"- {e.lossless_restatement}" for e in self._previous[:3])
        prior = ""
        if self._context_dialogues:
            prior = ("\n[Earlier conversation — CONTEXT ONLY, for resolving pronouns and "
                     "references. Do NOT create memory entries for these lines.]\n"
                     + "\n".join(str(d) for d in self._context_dialogues))
        return f"""Extract all valuable information from the dialogues into structured memory entries.
{ctx}{prior}

[Dialogues to extract]
{text}

[Requirements]
1. Cover all information; no pronouns or relative time (resolve to absolute).
2. Each lossless_restatement is a complete, self-contained sentence.
3. Extract keywords, timestamp (ISO 8601 or null), location, persons, entities, topic.

Return ONLY a JSON array of objects:
[{{"lossless_restatement": "...", "keywords": [...], "timestamp": null,
  "location": null, "persons": [...], "entities": [...], "topic": "..."}}]
"""
