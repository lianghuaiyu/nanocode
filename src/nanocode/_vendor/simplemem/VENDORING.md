# Vendoring: SimpleMem

This directory contains a vendored, text-only subset of **SimpleMem**.

| Field | Value |
| --- | --- |
| Upstream | https://github.com/aiming-lab/SimpleMem |
| License | MIT (Copyright (c) 2025 AIMING Lab) — see `LICENSE` in this directory |
| Vendored version | v0.3.0 |

## Why vendored

nanocode needs SimpleMem's hybrid (vector + BM25) text-memory retrieval as an
optional long-term memory backend (`--memory-backend simplemem`), without taking
on its heavy multimodal / benchmark / MCP dependencies. The upstream package
pulls in `torch`, `sentence-transformers`, and a large optional surface that we
deliberately do not want in nanocode's default install. We therefore vendor a
trimmed, text-only slice and inject our own embedding / LLM callables.

## Local patches (delta from upstream v0.3.0)

1. **Text-only trim.** Removed the multimodal path and everything that is not on
   the text-memory retrieval line:
   - multimodal ingestion / `OmniSimpleMem`
   - third-party integrations
   - the evolver benchmark / evaluation harnesses
   - the MCP server surface
   The remaining tree is `core/` (hybrid_retriever, memory_builder,
   answer_generator, database, models, utils, settings), `text/` (system),
   `config.py`, `router.py`, and `evolver/` kept only as imported by the retained
   code path.
2. **Absolute → relative imports.** All upstream absolute package imports
   (`simplemem....`) were rewritten to package-relative imports so the slice runs
   under `nanocode._vendor.simplemem` without the package being installed.
3. **Injected callable seams.** Added injection points so the backend supplies
   its own `embed_callable` and `llm_callable` (OpenAI-compatible, synchronous)
   instead of constructing in-process embedding/LLM clients. This is what lets
   nanocode drive SimpleMem with `NANOCODE_MEMORY_EMBED_*` / `NANOCODE_MEMORY_LLM_*`
   (or `OPENAI_*` fallback) configuration and avoid bundling `torch` /
   `sentence-transformers`.
4. **Removed omni registration.** The `mode="omni"` / OmniSimpleMem registration
   was dropped from the router; only `mode="text"` is reachable.
5. **stdout silencing is the caller's responsibility.** Upstream emits bare
   `print(...)` calls in several constructors/methods. These were left in place;
   the consuming `SimpleMemBackend` wraps every SimpleMem call in
   `contextlib.redirect_stdout(io.StringIO())` rather than patching the vendored
   source.

## How it is consumed

`nanocode/memory/backend.py` constructs the system via
`simplemem.create(mode="text", db_path=..., llm_callable=..., embed_callable=...,
embed_dimension=..., enable_planning=False, enable_reflection=False)` and wraps it
in `SimpleMemBackend`. Real-time recall calls `system.hybrid_retriever.retrieve(query)`
directly (planning/reflection disabled → pure vector + BM25, zero extra LLM calls).

## Updating

When re-syncing from upstream, re-apply the five patches above against the new
upstream revision and bump the **Vendored version** row. Keep `LICENSE` in sync
with the upstream license file.
