"""记忆系统的系统提示词片段构建。"""

from __future__ import annotations

from .store import load_memory_index, get_memory_dir


def build_memory_prompt_section() -> str:
    index = load_memory_index()
    memory_dir = str(get_memory_dir())

    return f"""# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user (include Why + How to apply)
- **project**: Ongoing work, goals, deadlines, decisions
- **reference**: Pointers to external resources (URLs, tools, dashboards)

## How to Save Memories
Use the `memory` tool with action="save":

```json
{{"action": "save", "name": "memory name", "type": "user|feedback|project|reference",
 "description": "one-line description", "content": "Memory content here."}}
```

The tool handles the filename, frontmatter, and MEMORY.md index for you — do NOT hand-craft
memory files with write_file, and do NOT update MEMORY.md manually.

## How to Recall, Read, and Manage
- `memory` action="recall" (query): find relevant memories on demand (add semantic=true for LLM-based selection).
- action="list": show the memory index.
- action="read" (filename): read one memory's full content.
- action="update" (filename, [content], [description]): revise an existing memory.
- action="delete" (filename): archive a memory (recoverable, not hard-deleted).

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Anything already in CLAUDE.md
- Ephemeral task details

## When to Recall
Relevant memories are surfaced automatically each turn. Beyond that, actively use
`memory` action="recall" whenever prior context would help — pull just-in-time instead of
relying only on what was pushed.
{chr(10) + "## Current Memory Index" + chr(10) + index if index else chr(10) + "(No memories saved yet.)"}"""
