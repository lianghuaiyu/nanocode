"""memory 工具 — schema only（docs/20 §2.3）。

memory 是完全 host-routed 的 capability 工具：CapabilityRouter 把它分发到
`host.execute_memory_tool(inp)` → `MemoryService.execute_tool(inp)`。本模块
**不** import store/backend、不写文件、不实现任何 action —— 只承载 schema 与文案
（Tool 以 run=None 注册）。
"""

from __future__ import annotations

SCHEMA = {
    "name": "memory",
    "description": (
        "Read and write your persistent, cross-session memory. Actions:\n"
        "- search(query): find the most relevant memories (fast, deterministic ranking).\n"
        "- read(ref): read one memory's full content by its reference.\n"
        "- list: list saved memory references.\n"
        "- add_note(title, content, [kind], [description]): explicitly record a "
        "durable fact (kind: user|feedback|project|reference).\n"
        "- stats: show memory backend status.\n"
        "- consolidate: run a host curator pass that cleans up memories "
        "(host/session operation; not available to sub-agents)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["search", "read", "list", "add_note", "stats", "consolidate"]},
            "query": {"type": "string", "description": "search: the search query"},
            "ref": {"type": "string", "description": "read: target memory reference"},
            "title": {"type": "string", "description": "add_note: short title/name"},
            "kind": {"type": "string", "enum": ["user", "feedback", "project", "reference", "note"],
                     "description": "add_note: memory kind"},
            "description": {"type": "string", "description": "add_note: one-line description"},
            "content": {"type": "string", "description": "add_note: memory body"},
            "limit": {"type": "number", "description": "search/list: max results"},
        },
        "required": ["action"],
    },
}


async def run(ctx, inp: dict) -> str:
    """host-routed：薄转发 ctx.memory.execute（docs/24 Phase 3）。

    consolidate 的子 agent 守卫留在 dispatch 咽喉点（router），与今天语义逐字一致——本 run
    只在守卫放行后被调用。
    """
    return await ctx.memory.execute(inp)
