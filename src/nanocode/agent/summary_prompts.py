"""agent/summary_prompts.py — 结构化 compaction / branch-summary prompt（docs/18 Phase 3）。

把"一句话摘要"升级为可在 0 上下文恢复工作的高质量结构化摘要：

- ``compact_prompt(custom_instructions)``：full compaction 的 no-tools + ``<analysis>``/``<summary>``
  结构化 prompt，含固定 section（Goal / Constraints / Progress / ... / read-files / modified-files）。
- ``partial_compact_prompt(custom_instructions)``：prompt-too-long retry 丢弃最旧 round 后的部分视图变体
  （告知模型这是被截断的前缀，仍按同一结构产出）。
- ``branch_summary_prompt(custom_instructions)``：/tree 切换时被离开 branch 的结构化摘要（Phase 7 用）。
- ``format_compact_summary(raw)``：写入 tree 前清理——优先取 ``<summary>`` 内文，否则剥离 ``<analysis>``。

要点（与 Claude Code compact/prompt.ts 对齐）：
- summarizer 是独立 no-tools LLM 调用——prompt 第一段明确"TEXT ONLY, do NOT call any tools"。
- read-files / modified-files 只列**真实经工具读/写**的文件；绝不从 repo map / 目录列表推断
  （docs/18 设计原则 4/5：repo map 不是已读事实）。
"""

from __future__ import annotations

import re

_NO_TOOLS = "Respond with TEXT ONLY. Do NOT call any tools."

_FILE_TRACKING_RULE = (
    "Only list a file under read-files / modified-files if it was genuinely READ or "
    "MODIFIED through tool calls in this conversation. Do NOT infer file activity from a "
    "repository map, directory listing, or mention — those are not read facts."
)

_COMPACT_SECTIONS = """## Goal
The user's overall objective and any sub-goals.

## Constraints & Preferences
Hard requirements, style/tooling preferences, and things to avoid.

## Progress
What has been accomplished so far.

## Key Decisions
Important choices made and the rationale behind them.

## Files and Code Sections
Specific files, functions, and code regions that were touched or are relevant, with a brief
note on each. Use real paths.

## Errors & Fixes
Errors encountered and how they were resolved (or that remain open).

## Current Work
What was being worked on at the exact moment of summarization.

## Next Steps
The concrete next actions to take.

## Critical Context
Anything else essential to continue the work without re-reading the full history.

## read-files
A bullet list of file paths actually READ via tools.

## modified-files
A bullet list of file paths actually CREATED or EDITED via tools."""


def _additional_instructions_block(custom_instructions: str | None) -> str:
    ci = (custom_instructions or "").strip()
    if not ci:
        return ""
    return ("\n\n## Additional Instructions\n"
            "The user provided extra instructions for this summary — follow them in addition to "
            "the structure above:\n" + ci)


def compact_prompt(custom_instructions: str | None = None) -> str:
    """Full compaction 的结构化 prompt（no-tools + <analysis>/<summary>）。"""
    return (
        f"{_NO_TOOLS}\n\n"
        "You are summarizing the conversation so far so that the work can continue in a fresh "
        "context window with no loss of essential information.\n\n"
        "First, inside <analysis> tags, think through the conversation chronologically: what the "
        "user asked for, what was explored, what was decided, what was built, what failed, and what "
        "remains. This analysis is scratch work and will be discarded.\n\n"
        "Then, inside <summary> tags, write a structured summary using EXACTLY these sections:\n\n"
        f"{_COMPACT_SECTIONS}\n\n"
        f"{_FILE_TRACKING_RULE}"
        f"{_additional_instructions_block(custom_instructions)}"
    )


def partial_compact_prompt(custom_instructions: str | None = None) -> str:
    """prompt-too-long retry 的部分视图变体：告知模型这是被截断的前缀（最旧消息已丢弃）。"""
    return (
        f"{_NO_TOOLS}\n\n"
        "NOTE: This is a PARTIAL view of the conversation — the oldest messages were dropped so the "
        "summary request fits the context window. Summarize what is visible; do not fabricate the "
        "dropped portion.\n\n"
        "Inside <analysis> tags, think through the visible conversation; then inside <summary> tags, "
        "write a structured summary using EXACTLY these sections:\n\n"
        f"{_COMPACT_SECTIONS}\n\n"
        f"{_FILE_TRACKING_RULE}"
        f"{_additional_instructions_block(custom_instructions)}"
    )


def branch_summary_prompt(custom_instructions: str | None = None) -> str:
    """被离开 branch 的结构化摘要（Phase 7）：让对话从另一点继续而无需重放该 branch。"""
    sections = """## Goal
The objective being pursued on the branch being left behind.

## Constraints & Preferences
Requirements and preferences surfaced on this branch.

## Progress (Done / In Progress / Blocked)
What was completed, in flight, and blocked on this branch.

## Key Decisions
Decisions made on this branch and their rationale.

## Next Steps
Concrete follow-ups this branch implied.

## Critical Context
Anything essential to carry forward (decisions, file paths, commands, tool observations).

## read-files
A bullet list of file paths actually READ via tools on this branch.

## modified-files
A bullet list of file paths actually CREATED or EDITED via tools on this branch."""
    return (
        f"{_NO_TOOLS}\n\n"
        "Summarize the abandoned branch below (a path this conversation explored and is now leaving) "
        "so the conversation can continue from a different point WITHOUT replaying that branch. "
        "Preserve decisions, file paths, commands, tool results, and unresolved work.\n\n"
        "Write the summary using EXACTLY these sections:\n\n"
        f"{sections}\n\n"
        f"{_FILE_TRACKING_RULE}"
        f"{_additional_instructions_block(custom_instructions)}"
    )


_ANALYSIS_RE = re.compile(r"<analysis>.*?</analysis>", re.DOTALL | re.IGNORECASE)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL | re.IGNORECASE)


def format_compact_summary(raw: str | None) -> str:
    """写入 tree 前清理 summary：优先取 <summary> 内文；否则剥离 <analysis> 块后返回剩余文本。

    幂等、对无标签输入安全（原样去空白返回）。tree 里的 compaction summary 绝不含 <analysis> 草稿。
    """
    if not raw:
        return raw or ""
    m = _SUMMARY_RE.search(raw)
    if m:
        return m.group(1).strip()
    return _ANALYSIS_RE.sub("", raw).strip()
