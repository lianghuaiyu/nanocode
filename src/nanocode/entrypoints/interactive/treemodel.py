"""entrypoints/interactive/treemodel.py — session entry-tree 的纯布局逻辑（移植自 pi tree-selector.ts）。

只吃 nanocode `tree.Entry` 列表 + 当前 leaf,产出可渲染的 `Row`（前缀连接线 / active-path 标记 /
当前 leaf 标记 / 内容预览）。**不碰 prompt_toolkit、不碰终端**——selector 外壳和文本回退都消费它,
故可脱离 Application 单测。

与 pi 的差异:pi 的「节点」是会话 entry;nanocode 的 entry 列表混有注解型（LEAF/LABEL/
SESSION_INFO/遥测）。这里用 `tree.leaf_id_after_entry(e) == e.id` 判定「可作为对话节点」——
即推进 leaf 的 entry 才进树,注解型不进（与 leaf 折叠规则同源,单一权威）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ...session import tree as T

FilterMode = Literal["default", "no-tools", "user-only", "labeled-only", "all"]
FILTER_ORDER: tuple[FilterMode, ...] = ("default", "no-tools", "user-only", "labeled-only", "all")

# 默认视图隐藏的「设置/记账」类型(pi isSettingsEntry)
_SETTINGS_TYPES = frozenset({
    T.LABEL, T.CUSTOM, T.MODEL_CHANGE, T.THINKING_LEVEL_CHANGE, T.SESSION_INFO,
})


# ─── 节点树 ───────────────────────────────────────────────────────────────────

@dataclass
class TreeNode:
    entry: T.Entry
    children: list["TreeNode"] = field(default_factory=list)


def _is_node(e: T.Entry) -> bool:
    """该 entry 是否为对话树节点(推进 leaf 者)——与 leaf 折叠规则同源。"""
    return T.leaf_id_after_entry(e) == e.id


def build_tree(entries: list[T.Entry]) -> list[TreeNode]:
    """从 entry 列表建对话节点树。父链指向非节点(如 session_start)者即为 root。"""
    nodes = {e.id: TreeNode(entry=e) for e in entries if _is_node(e)}
    roots: list[TreeNode] = []
    for nid, node in nodes.items():
        pid = node.entry.parentId
        parent = nodes.get(pid) if pid else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)
    return roots


def active_path_ids(entries: list[T.Entry], leaf_id: str | None) -> set[str]:
    """root→leaf 路径上的 entry id 集合(pi buildActivePath)。"""
    if leaf_id is None:
        return set()
    by_id = {e.id: e for e in entries}
    out: set[str] = set()
    cur = leaf_id
    while cur:
        out.add(cur)
        e = by_id.get(cur)
        if e is None:
            break
        cur = e.parentId
    return out


# ─── 过滤 + 剪枝(隐藏节点的子节点上挂到最近可见祖先,pi recalculateVisualStructure) ──

def _node_passes(node: TreeNode, mode: FilterMode, labels: dict[str, str]) -> bool:
    e = node.entry
    is_settings = e.type in _SETTINGS_TYPES
    role = (e.data.get("message") or {}).get("role") if e.type == T.MESSAGE else None
    if mode == "user-only":
        return e.type == T.MESSAGE and role == "user"
    if mode == "no-tools":
        return not is_settings and not (e.type == T.MESSAGE and role == "toolResult")
    if mode == "labeled-only":
        return e.id in labels
    if mode == "all":
        return True
    return not is_settings  # default


def prune_tree(roots: list[TreeNode], mode: FilterMode, labels: dict[str, str]) -> list[TreeNode]:
    """返回只含可见节点的新树:隐藏节点被摘除,其子节点上挂到最近可见祖先。"""
    def visit(node: TreeNode) -> list[TreeNode]:
        kept_children: list[TreeNode] = []
        for c in node.children:
            kept_children.extend(visit(c))
        if _node_passes(node, mode, labels):
            return [TreeNode(entry=node.entry, children=kept_children)]
        return kept_children  # 自己被隐藏 → 子节点提升到本层
    out: list[TreeNode] = []
    for r in roots:
        out.extend(visit(r))
    return out


# ─── 扁平化(pi flattenTree:active 分支排前 + 连接线 gutter) ─────────────────────

@dataclass
class _Gutter:
    position: int
    show: bool


@dataclass
class FlatNode:
    node: TreeNode
    indent: int
    show_connector: bool
    is_last: bool
    gutters: list[_Gutter]
    is_virtual_root_child: bool


def _contains_active(roots: list[TreeNode], leaf_id: str | None) -> dict[int, bool]:
    """post-order 标记每个节点的子树是否含 active leaf(用于把 active 分支排前)。"""
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


def flatten_tree(roots: list[TreeNode], leaf_id: str | None, multiple_roots: bool) -> list[FlatNode]:
    """扁平化为带缩进/连接线信息的 FlatNode 列表(忠实移植 pi flattenTree)。"""
    has_active = _contains_active(roots, leaf_id)
    result: list[FlatNode] = []

    def order_active_first(nodes: list[TreeNode]) -> list[TreeNode]:
        pri = [n for n in nodes if has_active.get(id(n))]
        rest = [n for n in nodes if not has_active.get(id(n))]
        return pri + rest

    ordered_roots = order_active_first(roots)
    # 栈项:(node, indent, just_branched, show_connector, is_last, gutters, is_virtual_root_child)
    stack: list[tuple] = []
    for i in range(len(ordered_roots) - 1, -1, -1):
        is_last = i == len(ordered_roots) - 1
        stack.append((ordered_roots[i], 1 if multiple_roots else 0, multiple_roots,
                      multiple_roots, is_last, [], multiple_roots))

    while stack:
        node, indent, just_branched, show_connector, is_last, gutters, is_vrc = stack.pop()
        result.append(FlatNode(node, indent, show_connector, is_last, gutters, is_vrc))

        children = order_active_first(node.children)
        multi = len(children) > 1
        # 只在真正的分叉点 +1 缩进;单子链(线性对话)与分叉子节点对齐在同一列,只靠 │ 续线区分。
        # （pi 源码还有「分叉后第一代再 +1」一档,但其 render 示意图其实是单列对齐;那一档会让单子
        #   链逐级右移、末分支孙节点失去续线而悬空——可读性差,故此处刻意不复刻,对齐 pi 示意图。）
        child_indent = indent + 1 if multi else indent

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


# ─── 行渲染(纯文本,无颜色;selector 自行上色,文本回退直接用) ────────────────────

@dataclass
class Row:
    entry: T.Entry
    prefix: str            # 连接线前缀(│ ├─ └─ + 空格)
    on_active_path: bool
    is_leaf: bool
    label: str | None
    content: str           # "user: …" / "assistant: …" / "[compaction: 3k tokens]" 等


def _build_prefix(fn: FlatNode, multiple_roots: bool) -> str:
    """逐格构造连接线前缀(pi render 的 prefixChars 循环)。"""
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
                out.append("─")
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


def entry_content(e: T.Entry) -> str:
    """节点的内容预览(纯文本,移植 pi getEntryDisplayText)。"""
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


def build_rows(entries: list[T.Entry], leaf_id: str | None, mode: FilterMode = "default") -> list[Row]:
    """顶层入口:entry 列表 + leaf + filter → 可渲染 Row 列表。selector 与文本回退共用。"""
    labels = T.labels_by_id(entries)
    roots = build_tree(entries)
    pruned = prune_tree(roots, mode, labels)
    multiple_roots = len(pruned) > 1
    flat = flatten_tree(pruned, leaf_id, multiple_roots)
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
        ))
    return rows


def render_tree_text(entries: list[T.Entry], leaf_id: str | None, mode: FilterMode = "default",
                     *, short_id: bool = True) -> list[str]:
    """纯文本渲染(非 TTY 回退 + 单测用)。格式:prefix • [label] short_id content ◀"""
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
