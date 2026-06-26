"""memory/prompts.py — backend-aware static memory guidance (docs/20 §3 Phase 3).

Replaces the old always-file-based static prompt builder. The markdown backend
still describes the file-based system; the SimpleMem backend describes an
indexed memory engine driven through the `memory` tool — it must never claim
there is a "file-based memory system at ...".
"""
from __future__ import annotations


def build_memory_prompt(backend) -> str:
    """Return static memory guidance shaped for the active backend."""
    name = getattr(backend, "name", "")
    if name == "markdown":
        return _markdown_prompt(backend)
    if name == "simplemem":
        return _simplemem_prompt(backend)
    return ""


def _markdown_prompt(backend) -> str:
    index = ""
    fn = getattr(backend, "memory_index", None)
    if callable(fn):
        index = fn() or ""
    from .store import get_memory_dir
    memory_dir = str(get_memory_dir())
    tail = ("\n## Current Memory Index\n" + index if index
            else "\n(No memories saved yet.)")
    return f"""# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user (include Why + How to apply)
- **project**: Ongoing work, goals, deadlines, decisions
- **reference**: Pointers to external resources (URLs, tools, dashboards)

## How to Save Memories
Use the `memory` tool with action="add_note":

```json
{{"action": "add_note", "title": "memory name", "kind": "user|feedback|project|reference",
 "description": "one-line description", "content": "Memory content here."}}
```

The tool handles the filename, frontmatter, and MEMORY.md index for you — do NOT hand-craft
memory files with write_file, and do NOT update MEMORY.md manually.

## How to Recall, Read, and Manage
- `memory` action="search" (query): find relevant memories on demand (deterministic ranking).
- action="list": show the memory index.
- action="read" (ref): read one memory's full content.

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Anything already in NANOCODE.md/AGENTS.md
- Ephemeral task details

## When to Recall
Relevant memories are surfaced automatically each turn. Beyond that, actively use
`memory` action="search" whenever prior context would help — pull just-in-time instead of
relying only on what was pushed.
{tail}"""


def _simplemem_prompt(backend) -> str:
    return """# Memory System

You have a persistent, indexed long-term memory. Memories are stored as compact,
self-contained entries (not files) and retrieved by relevance — you cannot edit
them by hand.

## How it works
- Relevant entries are surfaced automatically each turn via fast hybrid retrieval.
- Use the `memory` tool to interact with the index:
  - action="search" (query): find relevant entries on demand.
  - action="read" (ref): read one entry's full content by its reference.
  - action="list": list stored entry references.
  - action="add_note" (title, content, [kind]): explicitly record a durable fact.
  - action="stats": show backend status (entry count, index location).

## Rules
- Do NOT hand-write memory files with write_file — there is no file-based store.
- Prefer `add_note` for facts the user has confirmed or that you are confident are durable.
- Automatic recall may already surface relevant entries; search only when you need more.
"""
