"""session/context.py — fold + convert_to_llm（docs/13 §4 三段管线的前两段）。

    get_branch(leaf) → fold → AgentMessage[]（rich，含 compaction/branch_summary/custom 合成）
                      → convert_to_llm → 中立 Message[] → render(provider)[render.py]

fold 不是「纯拼接」（评审 M1）：要做标量 LWW + compaction 两区折叠 + 派生消息合成。
convert_to_llm 把派生消息降为中立 user 消息：summary 带 PREFIX/SUFFIX；custom_message **原样无 PREFIX**
（评审命中：否则会改写注入的 <system-reminder> 文本）。纯函数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import tree
from .tree import Entry

# 派生消息的 PREFIX/SUFFIX（仅 summary 类包裹；custom_message 原样）。
COMPACTION_PREFIX = "[Earlier conversation was summarized to save context. The summary follows.]\n\n"
COMPACTION_SUFFIX = "\n\n[End of summary. The conversation continues below.]"
BRANCH_SUMMARY_PREFIX = "[Summary of a branch this conversation explored and returned from:]\n\n"
BRANCH_SUMMARY_SUFFIX = "\n\n[End of branch summary.]"


@dataclass
class ScalarState:
    """分支折叠出的标量状态（LWW）。model 同时来自 model_change 与 assistant 消息（末者胜）。"""

    provider: str | None = None
    model_id: str | None = None
    thinking_level: str | None = None
    active_tools: list[str] | None = None


def _latest_compaction_index(branch: list[Entry]) -> int:
    """分支上最后一个 compaction 的下标；无则 -1（仅末个 compaction 生效，链式 compaction 自然复合）。"""
    idx = -1
    for i, e in enumerate(branch):
        if e.type == tree.COMPACTION:
            idx = i
    return idx


def fold(branch: list[Entry]) -> tuple[list[dict], ScalarState]:
    """折叠 root-first 分支 → (rich messages, 标量状态)。

    标量 pass：model（model_change 或 assistant 消息记录的 provider/model，末者胜）、
    thinkingLevel、activeTools，皆 LWW。
    消息 pass：若有 compaction C → 先放 compaction summary，再放 [0,C) 中 **firstKeptEntryId 起**
    的消息 + (C,end] 的全部消息（两区，评审 m12）；否则全分支。
    """
    scalar = ScalarState()
    for e in branch:
        if e.type == tree.MODEL_CHANGE:
            scalar.provider = e.data.get("provider", scalar.provider)
            scalar.model_id = e.data.get("modelId", scalar.model_id)
        elif e.type == tree.THINKING_LEVEL_CHANGE:
            scalar.thinking_level = e.data.get("thinkingLevel", scalar.thinking_level)
        elif e.type == tree.ACTIVE_TOOLS_CHANGE:
            tools = e.data.get("activeToolNames")
            if isinstance(tools, list):
                scalar.active_tools = list(tools)
        elif e.type == tree.MESSAGE:
            msg = e.data.get("message") or {}
            if msg.get("role") == "assistant":
                if msg.get("provider"):
                    scalar.provider = msg["provider"]
                if msg.get("model"):
                    scalar.model_id = msg["model"]

    comp_idx = _latest_compaction_index(branch)
    rich: list[dict] = []

    def emit(e: Entry) -> None:
        if e.type == tree.MESSAGE:
            m = e.data.get("message")
            if isinstance(m, dict):
                rich.append(m)
        elif e.type == tree.CUSTOM_MESSAGE:
            rich.append({
                "role": "custom",
                "customType": e.data.get("customType", ""),
                "content": e.data.get("content", ""),
                "display": bool(e.data.get("display", True)),
            })
        elif e.type == tree.BRANCH_SUMMARY:
            summary = (e.data.get("summary") or "").strip()
            if summary:
                rich.append({"role": "branchSummary", "summary": summary,
                             "fromId": e.data.get("fromId")})

    if comp_idx >= 0:
        comp = branch[comp_idx]
        rich.append({"role": "compactionSummary", "summary": comp.data.get("summary", ""),
                     "tokensBefore": comp.data.get("tokensBefore")})
        first_kept = comp.data.get("firstKeptEntryId")
        keeping = first_kept is None  # firstKeptEntryId 缺省 → 不丢前区（保守）
        for i in range(comp_idx):
            e = branch[i]
            if not keeping and e.id == first_kept:
                keeping = True
            if keeping:
                emit(e)
        for i in range(comp_idx + 1, len(branch)):
            emit(branch[i])
    else:
        for e in branch:
            emit(e)

    return rich, scalar


def _as_text(content: Any) -> str:
    """把 custom/summary 的 content 归一为字符串（dict-block list → 拼接 text）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def convert_to_llm(rich: list[dict]) -> list[dict]:
    """rich AgentMessage[] → 中立 Message[]（user/assistant/toolResult）。

    compactionSummary/branchSummary → user（带 PREFIX/SUFFIX）;
    custom → user（content **原样、无 PREFIX**，否则改写注入文本，评审命中）;
    user/assistant/toolResult → 原样透传。
    """
    out: list[dict] = []
    for m in rich:
        role = m.get("role")
        if role == "compactionSummary":
            out.append(tree.user_message(COMPACTION_PREFIX + (m.get("summary") or "") + COMPACTION_SUFFIX))
        elif role == "branchSummary":
            out.append(tree.user_message(BRANCH_SUMMARY_PREFIX + (m.get("summary") or "") + BRANCH_SUMMARY_SUFFIX))
        elif role == "custom":
            out.append(tree.user_message(m.get("content", "") if isinstance(m.get("content"), (str, list))
                                         else _as_text(m.get("content"))))
        elif role in ("user", "assistant", "toolResult"):
            out.append(m)
        # 其它（如未来 UI-only）默默跳过
    return out
