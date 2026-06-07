"""memory 工具：让模型主动读写持久化记忆（JIT）。

store 类 action（recall 廉价档 / list / read / save / update / delete）在此实现为
纯函数 run(inp)->str，走 tools.execute 分发。语义 recall（需 LLM side_query）由
agent.engine 拦截处理（见 _execute_tool_call），不在此文件。
"""

from __future__ import annotations

from .. import memory as _mem
from ..memory.store import (
    get_memory_dir, list_memories, save_memory, load_memory_index, VALID_TYPES,
)
from ..memory.maintenance import archive_file

SCHEMA = {
    "name": "memory",
    "description": (
        "Actively read and write your persistent, cross-session memory. Actions:\n"
        "- recall(query): find the most relevant memories for a query (keyword ranking; "
        "set semantic=true for LLM-based selection).\n"
        "- list: show the memory index.\n"
        "- read(filename): read one memory's full content.\n"
        "- save(name, type, description, content): create a memory "
        "(type: user|feedback|project|reference).\n"
        "- update(filename, [content], [description]): rewrite an existing memory.\n"
        "- delete(filename): archive a memory (recoverable, not hard-deleted).\n"
        "- consolidate: run a curator pass that proposes + applies cleanup "
        "(merge/rewrite/archive) across all memories; deletions are archived, not hard-deleted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["recall", "list", "read", "save", "update", "delete", "consolidate"]},
            "query": {"type": "string", "description": "recall: the search query"},
            "semantic": {"type": "boolean", "description": "recall: use LLM semantic selection instead of keyword ranking"},
            "filename": {"type": "string", "description": "read/update/delete: target memory filename"},
            "name": {"type": "string", "description": "save: memory name"},
            "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"], "description": "save: memory type"},
            "description": {"type": "string", "description": "save/update: one-line description"},
            "content": {"type": "string", "description": "save/update: memory body"},
            "limit": {"type": "number", "description": "recall: max results (default 5)"},
        },
        "required": ["action"],
    },
}


def _score(query: str, entry) -> int:
    """廉价确定性打分：query 词在 name/description/content 中的命中次数（name/desc 加权）。"""
    q = query.lower().split()
    if not q:
        return 0
    name = (entry.name or "").lower()
    desc = (entry.description or "").lower()
    body = (entry.content or "").lower()
    score = 0
    for w in q:
        score += 3 * name.count(w) + 2 * desc.count(w) + body.count(w)
    return score


def _recall_keyword(query: str, limit: int) -> str:
    entries = list_memories()
    if not entries:
        return "No memories saved yet."
    scored = [(e, _score(query, e)) for e in entries]
    hits = [(e, s) for e, s in scored if s > 0]
    hits.sort(key=lambda x: x[1], reverse=True)
    if not hits:
        return f"No memories matched: {query}"
    out = [f"Top {min(limit, len(hits))} memories for: {query}"]
    for e, s in hits[:limit]:
        out.append(f"\n[{e.type}] {e.name} ({e.filename})\n{e.description}\n{e.content}")
    return "\n".join(out)


def _read(filename: str) -> str:
    path = get_memory_dir() / filename
    if not path.exists():
        return f"Unknown memory: {filename}"
    return path.read_text()


def _save(inp: dict) -> str:
    name = inp.get("name")
    mtype = inp.get("type")
    if not name or not mtype:
        return "save requires 'name' and 'type'."
    if mtype not in VALID_TYPES:
        return f"Invalid type {mtype!r}. Must be one of: {', '.join(sorted(VALID_TYPES))}."
    fn = save_memory(name, inp.get("description", ""), mtype, inp.get("content", ""))
    return f"Saved memory: {fn}"


def _update(inp: dict) -> str:
    filename = inp.get("filename")
    if not filename:
        return "update requires 'filename'."
    entries = {e.filename: e for e in list_memories()}
    e = entries.get(filename)
    if e is None:
        return f"Unknown memory: {filename}"
    new_content = inp.get("content", e.content)
    new_desc = inp.get("description", e.description)
    save_memory(e.name, new_desc, e.type, new_content)
    return f"Updated memory: {filename}"


def _delete(inp: dict) -> str:
    filename = inp.get("filename")
    if not filename:
        return "delete requires 'filename'."
    ok = archive_file(filename, reason="deleted via memory tool")
    if not ok:
        return f"Unknown memory: {filename}"
    # 同步刷新 MEMORY.md 索引（archive_file 只移动文件，不更新索引）
    from ..memory.store import _update_memory_index
    _update_memory_index()
    return f"Archived memory: {filename} (recoverable)"


def run(inp: dict) -> str:
    action = inp.get("action", "")
    if action == "recall":
        # 语义档由 engine 拦截；走到这里说明是廉价档（或 engine 未拦截的回退）
        return _recall_keyword(inp.get("query", ""), int(inp.get("limit") or 5))
    if action == "list":
        idx = load_memory_index()
        return idx or "No memories saved yet."
    if action == "read":
        return _read(inp.get("filename", ""))
    if action == "save":
        return _save(inp)
    if action == "update":
        return _update(inp)
    if action == "delete":
        return _delete(inp)
    return f"Unknown memory action: {action}"
