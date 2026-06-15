"""session/branch_summary.py — Pi Branch Summarization（docs/18 Phase 7）。

/tree 切换 branch 时，把被离开（abandoned）的 branch 收敛成一条 branch_summary entry 注入新 branch，
使新 branch 只看到摘要、不看到旧 branch 的 raw messages（docs/18 设计原则 6）。

纯函数（除读 mgr 的树遍历外不写树）：
- collect_entries_for_branch_summary：找 old_leaf 与 target 的 deepest common ancestor，收集 old_leaf
  回溯到 DCA 之间的 abandoned entries（不在 compaction 边界停——compaction summary 本身进摘要输入）。
- prepare_branch_entries：从最新到最旧按 token 预算装入（保留最近 branch 信息）。
- serialize_branch_conversation：abandoned entries → 纯文本 transcript（tool result 限长，避免请求过大）。
- branch_file_tracking：从**全部** abandoned entries 累计 read/modified 文件（即使部分因预算没进 summarizer
  输入）——只看真实 tool call 参数 + 既有 compaction/branch_summary 的 details，绝不读 repo map。
"""

from __future__ import annotations

from . import tree as _tree

TOOL_RESULT_CAP = 2000   # branch transcript 里单条 tool result 的最大字符数


def _entry_tokens(e) -> int:
    from ..context.packs import estimate_tokens
    if e.type == _tree.MESSAGE:
        content = (e.data.get("message") or {}).get("content", "")
    elif e.type == _tree.CUSTOM_MESSAGE:
        content = e.data.get("content", "")
    elif e.type in (_tree.COMPACTION, _tree.BRANCH_SUMMARY):
        content = e.data.get("summary", "")
    else:
        return 0
    return estimate_tokens(content if isinstance(content, (str, list)) else str(content))


def collect_entries_for_branch_summary(mgr, old_leaf_id, target_id):
    """收集 abandoned entries + deepest common ancestor id。返回 (abandoned root-first, common_ancestor_id)。

    DCA = old_leaf branch 上最后一条仍在 target branch 内的 entry；abandoned = DCA 之后到 old_leaf 的全部。
    不在 compaction 边界停止（compaction summary 也是 abandoned 内容，应进摘要输入）。"""
    if old_leaf_id == target_id:
        return [], target_id
    try:
        current = mgr.get_branch(old_leaf_id)
        target = mgr.get_branch(target_id) if target_id is not None else []
    except Exception:
        return [], None
    target_ids = {e.id for e in target}
    common_idx = -1
    for i, e in enumerate(current):
        if e.id in target_ids:
            common_idx = i
    common_ancestor_id = current[common_idx].id if common_idx >= 0 else None
    return current[common_idx + 1:], common_ancestor_id


def prepare_branch_entries(entries, token_budget):
    """从最新到最旧按 token 预算装入，返回被选 entries（root-first）。

    至少保留最新一条（即使它单条已超预算——保证有内容可摘要）。file tracking 在调用方对**全部**
    abandoned 累计，不受此预算裁剪影响。"""
    chosen = []
    total = 0
    for e in reversed(entries):
        tok = _entry_tokens(e)
        if chosen and total + tok > token_budget:
            break
        chosen.append(e)
        total += tok
    chosen.reverse()
    return chosen


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def serialize_branch_conversation(entries) -> str:
    """abandoned entries → 纯文本 transcript（tool result 限 TOOL_RESULT_CAP 字符）。"""
    parts: list[str] = []
    for e in entries:
        if e.type == _tree.MESSAGE:
            msg = e.data.get("message") or {}
            role = msg.get("role") or "message"
            if role == "toolResult":
                text = _cap(_blocks_text(msg.get("content")), TOOL_RESULT_CAP)
                parts.append(f"tool[{msg.get('toolName', '')}]: {text}".strip())
            elif role == "assistant":
                blocks = msg.get("content") or []
                text = "".join(b.get("text", "") for b in blocks
                               if isinstance(b, dict) and b.get("type") == "text")
                tools = [b.get("name") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "toolCall"]
                if tools:
                    text = (text + "\n" if text else "") + "Tool calls: " + ", ".join(t for t in tools if t)
                parts.append(f"assistant: {text}".strip())
            else:
                parts.append(f"{role}: {_blocks_text(msg.get('content'))}".strip())
        elif e.type == _tree.CUSTOM_MESSAGE:
            parts.append(f"custom[{e.data.get('customType', '')}]: {e.data.get('content', '')}")
        elif e.type == _tree.COMPACTION:
            parts.append(f"compaction summary: {e.data.get('summary', '')}")
        elif e.type == _tree.BRANCH_SUMMARY:
            parts.append(f"branch summary: {e.data.get('summary', '')}")
    return "\n\n".join(p for p in parts if p.strip())


def _track_tool_call(name, args, read, modified) -> None:
    fp = (args or {}).get("file_path")
    if not fp:
        return
    if name == "read_file":
        read.add(fp)
    elif name in ("write_file", "edit_file"):
        modified.add(fp)
    # bash/run_shell：不解析命令参数（与 engine._on_file_touched 一致，仅 file_path 工具）。


def branch_file_tracking(entries):
    """从**全部** abandoned entries 累计 (readFiles, modifiedFiles)，sorted。

    来源：① assistant tool call 的 read/edit/write file_path 参数（真实工具调用）；② 既有
    compaction/branch_summary entry 的 details.readFiles/modifiedFiles（nested 累计）。绝不读 repo map。"""
    read: set = set()
    modified: set = set()
    for e in entries:
        if e.type in (_tree.COMPACTION, _tree.BRANCH_SUMMARY):
            d = e.data.get("details") or {}
            read.update(d.get("readFiles") or [])
            modified.update(d.get("modifiedFiles") or [])
        elif e.type == _tree.MESSAGE:
            msg = e.data.get("message") or {}
            if msg.get("role") == "assistant":
                for b in (msg.get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "toolCall":
                        _track_tool_call(b.get("name"), b.get("arguments"), read, modified)
    return sorted(read), sorted(modified)
