"""agent/tool_result_budget.py — per-message 聚合 tool-result 预算（docs/18 Phase 6）。

借 Claude Code toolResultStorage 思路：单条 tool result 已有 per-tool cap（compaction.persist_large_result
30KB 落盘 + read_file 自身分页/256KB），但**多个并行 tool result** 经 render 合并进**同一条** API user
message（anthropic _render_anthropic 把连续 toolResult 并成一条 user）后仍可能整体打爆该消息。这里在
请求装配（render 前）对**请求局部**的中立消息列表加一层聚合预算：

- 按 render 的分组语义聚合连续 role=='toolResult' 消息为一组；
- 对**新鲜**超预算 group，优先替换最大的 tool result 为稳定 preview（按 toolCallId 缓存、复用同一 preview）；
- 已替换的 toolCallId 每次复用同一 preview；**已见但未替换**的 toolCallId **冻结**（绝不后补替换）——
  保护 anthropic prompt-cache 前缀稳定（决策单调、按 toolCallId append-only）；
- read_file 跳过替换（自身已分页/cap），但仍标记 seen。

不变量：只操作**请求局部副本**，绝不改写树 entry（树存干净原文）；replacement 是 read-time projection。
第一阶段仅 main-thread runtime 内稳定（不持久化到树/sidecar）；resume 稳定复原留作第二阶段。
"""

from __future__ import annotations

from dataclasses import dataclass, field

_TAIL_KEEP_TOOLS = frozenset({"run_shell", "sandbox_shell"})
DEFAULT_SKIP_TOOLS = frozenset({"read_file"})


@dataclass
class ContentReplacementState:
    """per-session 的 tool-result 替换决策（按 toolCallId 冻结，跨 turn 稳定）。"""

    seen_ids: set = field(default_factory=set)
    replacements: dict = field(default_factory=dict)   # toolCallId -> stable preview str


def _content_str(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return str(content) if content is not None else ""


def default_preview_builder(tool_name: str, content: str, *, keep_chars: int = 1000) -> str:
    """稳定（确定性）preview：非 shell 工具留头、shell 工具留尾（错误常在末尾）。"""
    size = len(content)
    if tool_name in _TAIL_KEEP_TOOLS:
        snippet, label = content[-keep_chars:], f"last {keep_chars} chars"
    else:
        snippet, label = content[:keep_chars], f"first {keep_chars} chars"
    return (f"[Large tool result elided to fit the per-message context budget "
            f"({size} chars; full output remains in session history). Showing {label}:]\n{snippet}")


def _with_content(msg: dict, content: str) -> dict:
    m = dict(msg)
    m["content"] = content
    return m


def apply_tool_result_budget(messages, state: ContentReplacementState, *,
                             per_group_token_budget: int,
                             skip_tools=DEFAULT_SKIP_TOOLS,
                             preview_builder=default_preview_builder):
    """对请求局部中立消息列表施加 per-message 聚合 tool-result 预算。返回**新列表**（不改 input dicts）。

    分组：连续 role=='toolResult' 的消息（= render 合并成一条 anthropic user 的那批）。"""
    from ..context.packs import estimate_tokens

    def _size(content) -> int:
        return estimate_tokens(content if isinstance(content, (str, list)) else _content_str(content))

    out: list = []
    i, n = 0, len(messages)
    while i < n:
        if (messages[i] or {}).get("role") != "toolResult":
            out.append(messages[i])
            i += 1
            continue
        group = []
        while i < n and (messages[i] or {}).get("role") == "toolResult":
            group.append(messages[i])
            i += 1
        out.extend(_budget_group(group, state, per_group_token_budget, skip_tools,
                                 preview_builder, _size))
    return out


def _budget_group(group, state, budget, skip_tools, preview_builder, size_of):
    # ① 先施加既有替换决策（稳定复用同一 preview）
    result = []
    for m in group:
        tcid = m.get("toolCallId")
        if tcid in state.replacements:
            result.append(_with_content(m, state.replacements[tcid]))
        else:
            result.append(m)

    def total() -> int:
        return sum(size_of(x.get("content", "")) for x in result)

    # ② 仍超预算 → 只替换**新鲜**（未 seen）且非 skip 的 toolCallId，按 size 降序
    if total() > budget:
        candidates = []
        for idx, m in enumerate(group):
            tcid = m.get("toolCallId")
            if tcid in state.replacements:
                continue
            if tcid in state.seen_ids:
                continue                    # 已见未替换 → 冻结，绝不后补
            if m.get("toolName") in skip_tools:
                continue                    # read_file 等自身已 cap → 跳过替换
            candidates.append((size_of(m.get("content", "")), idx, m))
        candidates.sort(key=lambda x: x[0], reverse=True)
        for _size, idx, m in candidates:
            if total() <= budget:
                break
            content_str = _content_str(m.get("content"))
            preview = preview_builder(m.get("toolName", ""), content_str)
            if size_of(preview) >= size_of(m.get("content", "")):
                continue                    # preview 不比原文小 → 替换无益（小结果），跳过
            state.replacements[m.get("toolCallId")] = preview
            result[idx] = _with_content(m, preview)

    # ③ 冻结本组**每个** toolCallId 本轮的渲染（full 或 preview）——含 under-budget 时以全文发出的：
    # 决策一经定即不可变（否则日后预算变小把全文改成 preview → 改写已缓存前缀，击穿 prompt cache）。
    for m in group:
        if m.get("toolCallId"):
            state.seen_ids.add(m.get("toolCallId"))
    return result
