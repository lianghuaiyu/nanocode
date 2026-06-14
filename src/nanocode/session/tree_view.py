"""Session entry tree projection for /tree and /fork pages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from . import tree as T

FilterMode = Literal["default", "no-tools", "user-only", "labeled-only", "all"]
FILTER_ORDER: tuple[FilterMode, ...] = ("default", "no-tools", "user-only", "labeled-only", "all")


@dataclass
class TreeNode:
    entry: T.Entry
    children: list["TreeNode"] = field(default_factory=list)


def _is_node(e: T.Entry) -> bool:
    return T.leaf_id_after_entry(e) == e.id


def build_tree(entries: list[T.Entry]) -> list[TreeNode]:
    nodes = {e.id: TreeNode(e) for e in entries if _is_node(e)}
    roots: list[TreeNode] = []
    for node in nodes.values():
        parent = nodes.get(node.entry.parentId or "")
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
    return roots


def active_path_ids(entries: list[T.Entry], leaf_id: str | None) -> set[str]:
    by_id = {e.id: e for e in entries}
    out: set[str] = set()
    cur = leaf_id
    while cur:
        e = by_id.get(cur)
        if e is None:
            break
        if _is_node(e):
            out.add(e.id)
        cur = e.parentId
    return out


def _node_passes(node: TreeNode, mode: FilterMode, labels: dict[str, str]) -> bool:
    e = node.entry
    if mode == "all":
        return True
    if mode == "labeled-only":
        return e.id in labels
    if mode == "user-only":
        return e.type == T.MESSAGE and (e.data.get("message") or {}).get("role") == "user"
    if mode == "no-tools":
        return not (e.type == T.MESSAGE and (e.data.get("message") or {}).get("role") == "toolResult")
    if e.type != T.MESSAGE:
        return e.id in labels
    role = (e.data.get("message") or {}).get("role")
    if role == "toolResult":
        return False
    return True


def prune_tree(roots: list[TreeNode], mode: FilterMode, labels: dict[str, str]) -> list[TreeNode]:
    def visit(node: TreeNode) -> list[TreeNode]:
        children: list[TreeNode] = []
        for c in node.children:
            children.extend(visit(c))
        if _node_passes(node, mode, labels):
            return [TreeNode(node.entry, children)]
        return children

    out: list[TreeNode] = []
    for root in roots:
        out.extend(visit(root))
    return out


@dataclass
class _Gutter:
    position: int
    show: bool


@dataclass
class FlatNode:
    node: TreeNode
    indent: int
    is_last: bool
    gutters: list[_Gutter]
    show_connector: bool
    is_virtual_root_child: bool
    foldable: bool = False
    folded: bool = False


def _contains_active(roots: list[TreeNode], leaf_id: str | None) -> dict[int, bool]:
    has: dict[int, bool] = {}
    order: list[TreeNode] = []
    stack = list(roots)
    while stack:
        n = stack.pop()
        order.append(n)
        stack.extend(n.children)
    for n in reversed(order):
        h = leaf_id is not None and n.entry.id == leaf_id
        for c in n.children:
            if has.get(id(c)):
                h = True
        has[id(n)] = h
    return has


def _walk_nodes(nodes: list[TreeNode]):
    for n in nodes:
        yield n
        yield from _walk_nodes(n.children)


def flatten_tree(roots: list[TreeNode], leaf_id: str | None, multiple_roots: bool,
                 folded: "set[str] | frozenset[str]" = frozenset()) -> list[FlatNode]:
    has_active = _contains_active(roots, leaf_id)
    result: list[FlatNode] = []

    def order_active_first(nodes: list[TreeNode]) -> list[TreeNode]:
        pri = [n for n in nodes if has_active.get(id(n))]
        rest = [n for n in nodes if not has_active.get(id(n))]
        return pri + rest

    ordered_roots = order_active_first(roots)
    stack: list[tuple] = []
    for i in range(len(ordered_roots) - 1, -1, -1):
        is_last = i == len(ordered_roots) - 1
        stack.append((ordered_roots[i], 1 if multiple_roots else 0, multiple_roots,
                      multiple_roots, is_last, [], multiple_roots))

    while stack:
        node, indent, just_branched, show_connector, is_last, gutters, is_vrc = stack.pop()
        children = order_active_first(node.children)
        foldable = len(children) > 0
        is_folded = foldable and node.entry.id in folded
        result.append(FlatNode(node, indent, is_last, gutters, show_connector, is_vrc,
                               foldable=foldable, folded=is_folded))
        if is_folded:
            continue  # 折叠：不展开子节点

        multi = len(children) > 1
        if multi:
            child_indent = indent + 1
        elif just_branched and indent > 0:
            child_indent = indent + 1
        else:
            child_indent = indent

        connector_displayed = show_connector and not is_vrc
        cur_display_indent = max(0, indent - 1) if multiple_roots else indent
        connector_pos = max(0, cur_display_indent - 1)
        child_gutters = ([*gutters, _Gutter(connector_pos, not is_last)]
                         if connector_displayed else gutters)

        for i in range(len(children) - 1, -1, -1):
            child_is_last = i == len(children) - 1
            stack.append((children[i], child_indent, multi, multi, child_is_last,
                          child_gutters, False))
    return result


@dataclass
class Row:
    entry: T.Entry
    prefix: str
    on_active_path: bool
    is_leaf: bool
    label: str | None
    content: str
    foldable: bool = False
    folded: bool = False


def _build_prefix(fn: FlatNode, multiple_roots: bool) -> str:
    display_indent = max(0, fn.indent - 1) if multiple_roots else fn.indent
    connector = (fn.is_last and "└─ " or "├─ ") if (fn.show_connector and not fn.is_virtual_root_child) else ""
    connector_pos = (display_indent - 1) if connector else -1
    total = display_indent * 3
    out: list[str] = []
    for i in range(total):
        level = i // 3
        pos = i % 3
        gutter = next((g for g in fn.gutters if g.position == level), None)
        if gutter is not None:
            out.append("│" if (pos == 0 and gutter.show) else " ")
        elif connector and level == connector_pos:
            if pos == 0:
                out.append("└" if fn.is_last else "├")
            elif pos == 1:
                # 折叠态字形（Pi tree-selector.ts:653-662）：⊞ 已折 / ⊟ 可折(展开) / ─ 不可折
                out.append("⊞" if fn.folded else ("⊟" if fn.foldable else "─"))
            else:
                out.append(" ")
        else:
            out.append(" ")
    return "".join(out)


def _normalize(s: str) -> str:
    return s.replace("\n", " ").replace("\t", " ").strip()


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_names(content) -> list[str]:
    if isinstance(content, list):
        return [b.get("name", "tool") for b in content
                if isinstance(b, dict) and b.get("type") == "toolCall"]
    return []


def _content_block_types(content) -> list[str]:
    if isinstance(content, list):
        return [str(b.get("type", "?")) for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return ["text"]
    return []


def entry_content(e: T.Entry) -> str:
    if e.type == T.MESSAGE:
        msg = e.data.get("message") or {}
        role = msg.get("role")
        if role == "user":
            return "user: " + _normalize(_extract_text(msg.get("content")))
        if role == "assistant":
            txt = _normalize(_extract_text(msg.get("content")))
            if txt:
                return "assistant: " + txt
            tools = _tool_names(msg.get("content"))
            if tools:
                return "assistant: [" + ", ".join(tools) + "]"
            if msg.get("stopReason") == "aborted":
                return "assistant: (aborted)"
            return "assistant: (no content)"
        if role == "toolResult":
            return "[" + str(msg.get("toolName") or "tool") + "]"
        return f"[{role}]"
    if e.type == T.COMPACTION:
        tb = e.data.get("tokensBefore") or 0
        return f"[compaction: {round(tb / 1000)}k tokens]"
    if e.type == T.BRANCH_SUMMARY:
        return "[branch summary]: " + _normalize(e.data.get("summary") or "")
    if e.type == T.MODEL_CHANGE:
        return f"[model: {e.data.get('modelId', '?')}]"
    if e.type == T.THINKING_LEVEL_CHANGE:
        return f"[thinking: {e.data.get('thinkingLevel', '?')}]"
    if e.type == T.CUSTOM:
        return f"[custom: {e.data.get('customType', '?')}]"
    return f"[{e.type}]"


def entry_kind(e: T.Entry) -> str:
    if e.type == T.MESSAGE:
        role = (e.data.get("message") or {}).get("role")
        return f"message:{role or '?'}"
    return e.type


def entry_detail_lines(row: "Row") -> list[str]:
    """Readable metadata + body for the selected tree row."""

    e = row.entry
    lines = [
        f"type     {entry_kind(e)}",
        f"id       {e.id}",
        f"parent   {e.parentId or '(root)'}",
        f"time     {e.timestamp or '?'}",
        f"branch   {'active path' if row.on_active_path else 'side branch'}"
        + (" · leaf" if row.is_leaf else ""),
    ]
    if row.label:
        lines.append(f"label    {row.label}")

    if e.type == T.MESSAGE:
        msg = e.data.get("message") or {}
        role = msg.get("role") or "?"
        lines.append(f"role     {role}")
        if msg.get("timestamp") and msg.get("timestamp") != e.timestamp:
            lines.append(f"msg time {msg.get('timestamp')}")
        if role == "assistant":
            if msg.get("model"):
                lines.append(f"model    {msg.get('model')}")
            if msg.get("stopReason"):
                lines.append(f"stop     {msg.get('stopReason')}")
            if msg.get("latencyMs") is not None:
                lines.append(f"latency  {msg.get('latencyMs')}ms")
            usage = msg.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("input_tokens") or usage.get("inputTokens")
                out = usage.get("output_tokens") or usage.get("outputTokens")
                if inp is not None or out is not None:
                    lines.append(f"usage    in={inp or 0} out={out or 0}")
            tools = _tool_names(msg.get("content"))
            if tools:
                lines.append("tools    " + ", ".join(tools))
        elif role == "toolResult":
            lines.append(f"tool     {msg.get('toolName') or '?'}")
            lines.append(f"call id  {msg.get('toolCallId') or '?'}")
            lines.append(f"error    {bool(msg.get('isError'))}")
            if msg.get("latencyMs") is not None:
                lines.append(f"latency  {msg.get('latencyMs')}ms")

        blocks = _content_block_types(msg.get("content"))
        if blocks:
            lines.append("blocks   " + ", ".join(blocks))

    elif e.type == T.COMPACTION:
        lines.append(f"tokens   {e.data.get('tokensBefore') or 0}")
        if e.data.get("kind"):
            lines.append(f"kind     {e.data.get('kind')}")
        if e.data.get("firstKeptEntryId"):
            lines.append(f"kept     {e.data.get('firstKeptEntryId')}")
    elif e.data:
        try:
            payload = json.dumps(e.data, ensure_ascii=False, sort_keys=True)
        except TypeError:
            payload = str(e.data)
        lines.append(f"data     {payload[:180]}")

    body = entry_content(e)
    if e.type == T.MESSAGE:
        msg = e.data.get("message") or {}
        full = _extract_text(msg.get("content"))
        if full:
            body = full
    lines.extend(["", "preview", body or "(empty)"])
    return lines


def build_rows(entries: list[T.Entry], leaf_id: str | None, mode: FilterMode = "default",
               folded: "set[str] | frozenset[str]" = frozenset()) -> list[Row]:
    labels = T.labels_by_id(entries)
    roots = build_tree(entries)
    pruned = prune_tree(roots, mode, labels)
    multiple_roots = len(pruned) > 1
    flat = flatten_tree(pruned, leaf_id, multiple_roots, folded)
    active = active_path_ids(entries, leaf_id)
    rows: list[Row] = []
    for fn in flat:
        e = fn.node.entry
        rows.append(Row(
            entry=e,
            prefix=_build_prefix(fn, multiple_roots),
            on_active_path=e.id in active,
            is_leaf=(e.id == leaf_id),
            label=labels.get(e.id),
            content=entry_content(e),
            foldable=fn.foldable,
            folded=fn.folded,
        ))
    return rows


def render_tree_text(entries: list[T.Entry], leaf_id: str | None, mode: FilterMode = "default",
                     *, short_id: bool = True) -> list[str]:
    rows = build_rows(entries, leaf_id, mode)
    if not rows:
        return ["  (no entries)"]
    out: list[str] = []
    for r in rows:
        marker = "• " if r.on_active_path else "  "
        sid = ("…" + r.entry.id[-8:] + " ") if short_id else ""
        lbl = f"[{r.label}] " if r.label else ""
        tail = "  ◀ current" if r.is_leaf else ""
        out.append(f"  {r.prefix}{marker}{lbl}{sid}{r.content}{tail}")
    return out
