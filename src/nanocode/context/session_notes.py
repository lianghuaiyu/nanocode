"""context/session_notes.py — Session memory / rolling notes 的**接口预留**（docs/18 Phase 8）。

⚠️ 第一阶段刻意**不实现**（留桩）。这里只锚定 Claude Code SessionMemory 思路的 schema 形状，使后续
落地时不必再改 packs/cache_policy 词表：

- provider kind：``SESSION_NOTES_KIND``（"session_notes"）。
- lifecycle = "session"（→ survives_compaction=True，与 project_instructions 同级跨压缩存活）。
- persist_policy = "custom_message"（经唯一持久注入通道落树）或单独 notes 文件，二选一后再实现。
- cache_policy = "append_only"（追加在稳定前缀之后，不破缓存）。
- 预算：总上限 ``SESSION_NOTES_TOTAL_TOKENS``(12k)，单 section 上限 ``SESSION_NOTES_SECTION_TOKENS``(2k)。
- section 顺序见 ``SESSION_NOTES_SECTIONS``。

第一阶段明确**不做**（docs/18 §8）：
- 不引入 forked agent 自动编辑 notes 文件；
- 不让 session notes 替代 compaction summary；
- 不把 repo map 写入 notes。

构造一个真正的 SessionNotesProvider 会 raise NotImplementedError——本桩只保留契约，不接线进
default_providers / 任何 turn 路径。
"""

from __future__ import annotations

SESSION_NOTES_KIND = "session_notes"

# survival / cache 契约（与 packs.Lifecycle/CachePolicy/PersistPolicy 词表对齐，无需改 schema）。
SESSION_NOTES_LIFECYCLE = "session"
SESSION_NOTES_PERSIST_POLICY = "custom_message"
SESSION_NOTES_CACHE_POLICY = "append_only"

# 预算（docs/18 §8）：总 12k、单 section 2k。落地时对 packs.estimate_tokens 强制。
SESSION_NOTES_TOTAL_TOKENS = 12_000
SESSION_NOTES_SECTION_TOKENS = 2_000

SESSION_NOTES_SECTIONS = (
    "Session Title",
    "Current State",
    "Task Specification",
    "Files and Functions",
    "Workflow",
    "Errors & Corrections",
    "Codebase and System Documentation",
    "Learnings",
    "Key Results",
    "Worklog",
)


class SessionNotesProvider:  # pragma: no cover - 接口预留桩，未实现
    """预留：session notes / rolling memory provider。第一阶段未实现。

    落地时应：lifecycle=session、persist_policy=custom_message（或独立 notes 文件）、cache_policy=
    append_only，按上述 budget/section 产出 ContextPack；并经 inject 路径注入（survives_compaction）。"""

    id = SESSION_NOTES_KIND
    enable_attr = "include_session_notes"

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "SessionNotesProvider is reserved (docs/18 Phase 8) and not implemented in this phase.")
