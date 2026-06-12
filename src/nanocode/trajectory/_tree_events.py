"""trajectory._tree_events — canonical session 树 → TrajEvent 流（Milestone B2 适配器）。

把 ``session.jsonl`` canonical 树（+ 子会话 fan-out）重建为一段与旧 ``events.reader.
merge_session_events`` **形状等价**的事件流，供既有 projection / metrics / eval 投影逻辑消费。
B1 已把原本只进 wire 的派生遥测（usage/latency、llm_request sizing、turn_end、tool_blocked、
budget_exceeded、permission_decision、compaction）落进树——本适配器据此**无损**重建事件流。

硬边界（三层，见 trajectory/__init__.py）：
- 只 import ``session.manager`` / ``session.tree`` 的**只读** API + 本包 ``_text``；**绝不** import
  ``events.*``（B3 将删除）、``trace.*``、任何 runtime（agent.*）。
- ``TrajEvent`` 自包含（不继承 events.models.SessionEvent），暴露 projection/metrics/eval 读到的
  全部属性：``type / agent_id / branch_id / seq / ts / turn_id / id / parent_id /
  parent_event_id / session_id / data``。
- 派生标签 reward / eval_result 绝不出现在 TrajEvent.data（它们是 Step-only，由 eval 回填）。

容错铁律：缺失/坏会话、缺字段 entry 都**绝不**令重建崩溃——降级为空流或缺字段事件。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..session import tree as T
from ..session.manager import SessionManager, children, session_file
from ..paths import sessions_dir

# 多 agent fan-out：父会话事件归 "main"；子会话归其 parentSession.agentId（连字符，如 agent-001）。
_MAIN_AGENT = "main"


@dataclass
class TrajEvent:
    """一条树派生事件（SessionEvent 形状的精简自包含视图）。

    projection/metrics/eval 只读这些属性；``seq`` 是**每 agent 单调递增的写入序**
    （取代 wire 的 per-agent seq），保证 step_id（``step_{agent}_{seq}``）唯一且
    ``_step_anchor_seq`` 的就近归属仍成立。``line_no`` 仅供与 SessionEvent 接口对齐
    （metrics 排序键用到），此处 = seq。
    """

    type: str
    agent_id: str
    seq: int
    ts: str
    session_id: str
    id: "str | None" = None
    parent_id: "str | None" = None
    parent_event_id: "str | None" = None
    branch_id: str = "main"
    turn_id: "str | None" = None
    line_no: int = 0
    data: dict = field(default_factory=dict)


# ─── 树 entry → TrajEvent(s) 重建 ─────────────────────────────────────────────


def _as_text(content) -> str:
    """把中立 Message 的 content（str / block list）归一为纯文本（拼接 text 块）。绝不抛。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return ""


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class _SeqGen:
    """每 agent 单调递增 seq 发号器（branch 序）。"""

    def __init__(self) -> None:
        self._n = -1

    def next(self) -> int:
        self._n += 1
        return self._n


# 不推进对话 branch 的 entry 类型（header / 注解 / leaf 移动）——它们不是 branch DAG 的节点。
# 与 tree.leaf_id_after_entry 的 _UNCHANGED 集 + LEAF 对齐。
_NON_BRANCH_TYPES = frozenset({
    T.SESSION_START, T.LABEL, T.SESSION_INFO, T.LEAF,
    T.PERMISSION_DECISION, T.TOOL_BLOCKED, T.BUDGET_EXCEEDED, T.TURN_END, T.SESSION_END, T.LLM_REQUEST,
})


def _assign_branches(entries: "list[T.Entry]") -> "tuple[dict, dict]":
    """从 canonical 树派生每个 conversation entry 的 branch_id + fork parent（取代硬编码 "main"）。

    conversation DAG 只由「推进 leaf」的 entry 组成（MESSAGE/COMPACTION/CUSTOM_MESSAGE…）；注解型
    entry（LLM_REQUEST/TURN_END/permission…）不入 DAG（它们 parentId=写时 leaf、不推进 leaf）。
    fork = 某 conv parent 有 >1 个 conv 子（in-file /fork、/tree 导航后续写、clone）；/clear→set_leaf(None)
    后的新对话 parentId=None → 第二个 root → 新 branch。第一个 root = "main"；额外 branch = b1/b2…。

    返回 (branch_of: conv entry_id→branch_id, branch_fork_point: branch_id→fork point parent entry id)。
    fork point 按 **branch_id** 记（而非 branch 起点 entry id）——因 branch 起点可能是 user MESSAGE，它不产
    projection event，故 parent_event_id 须挂到该 branch **首个 emitted** event 上（见 _branch_events）。
    """
    is_branch = {e.id: (e.type not in _NON_BRANCH_TYPES) for e in entries}
    seen_conv_children: dict = {}        # parent_id → 已见 conv 子计数
    branch_of: dict = {}
    branch_fork_point: dict = {}
    counter = [0]

    def _new_branch() -> str:
        b = "main" if counter[0] == 0 else f"b{counter[0]}"
        counter[0] += 1
        return b

    for e in entries:
        if not is_branch.get(e.id):
            continue
        p = e.parentId
        if p is None or not is_branch.get(p):
            branch_of[e.id] = _new_branch()      # root conv 节点（首个=main；/clear 后的新对话=b{n}）
        else:
            n = seen_conv_children.get(p, 0)
            seen_conv_children[p] = n + 1
            if n == 0:
                branch_of[e.id] = branch_of.get(p, "main")   # 首子继承 parent branch
            else:
                nb = _new_branch()                           # 兄弟分叉 → 新 branch
                branch_of[e.id] = nb
                branch_fork_point[nb] = p                    # fork point（供 project parent_event_id 链接）
    return branch_of, branch_fork_point


def _emit_for_entry(entry: T.Entry, *, agent_id: str, session_id: str, seqgen: _SeqGen,
                    branch_id: str, turn_id: "str | None") -> "list[TrajEvent]":
    """把一条树 entry 重建为 0+ 条 TrajEvent（与旧 wire 配对一致）。绝不抛。
    branch_id / turn_id 由 _branch_events 据树 fork 结构 + turn 边界派生（取代硬编码）；
    branch 起点的 parent_event_id 由 _branch_events 在返回的首个 event 上回填。"""
    out: list[TrajEvent] = []
    etype = entry.type
    data = entry.data if isinstance(entry.data, dict) else {}
    ts = entry.timestamp or ""

    def mk(ev_type: str, ev_data: dict) -> TrajEvent:
        seq = seqgen.next()
        return TrajEvent(
            type=ev_type, agent_id=agent_id, seq=seq, ts=ts,
            session_id=session_id, id=entry.id, parent_id=entry.parentId,
            branch_id=branch_id, turn_id=turn_id, line_no=seq, data=ev_data,
        )

    if etype == T.LLM_REQUEST:
        out.append(mk("llm_request", {
            "model": data.get("model"),
            "message_count": data.get("messageCount"),
            "messages_chars": data.get("messagesChars"),
        }))
        return out

    if etype == T.MESSAGE:
        msg = data.get("message")
        if not isinstance(msg, dict):
            return out
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            tool_uses = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "toolCall":
                    tool_uses.append({
                        "id": b.get("id"),
                        "name": b.get("name"),
                        "input": b.get("arguments") if isinstance(b.get("arguments"), dict)
                        else (b.get("arguments") or {}),
                    })
            text = _as_text(content)
            # assistant_message（含 tool_uses）→ llm_response（usage+latency）→ 每个 toolCall 一条 tool_call。
            # 顺序：assistant 在 response 前（projection._find_next_llm_response 从 assistant 之后找 response）；
            # tool_call 在 assistant 之后（_preceding_assistant_summary 向上找 assistant 作 observation）。
            out.append(mk("assistant_message", {"text": text, "tool_uses": tool_uses}))
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            out.append(mk("llm_response", {
                "input_tokens": usage.get("inputTokens"),
                "output_tokens": usage.get("outputTokens"),
                "latency_ms": _int_or_none(msg.get("latencyMs")),
            }))
            for tu in tool_uses:
                out.append(mk("tool_call", {
                    "tool": tu.get("name"),
                    "tool_use_id": tu.get("id"),
                    "input": tu.get("input"),
                }))
            return out
        if role == "toolResult":
            out.append(mk("tool_result", {
                "tool": msg.get("toolName"),
                "tool_use_id": msg.get("toolCallId"),
                "result": _as_text(msg.get("content")),
                "latency_ms": _int_or_none(msg.get("latencyMs")),
                "is_error": bool(msg.get("isError", False)),
            }))
            return out
        # user / 其它 role：不产 projection 事件（observation 由 llm_request 派生）。
        return out

    if etype == T.PERMISSION_DECISION:
        out.append(mk("permission_decision", {
            "tool": data.get("tool"),
            "action": data.get("action"),
            "message": data.get("message"),
        }))
        return out

    if etype == T.TOOL_BLOCKED:
        out.append(mk("tool_blocked", {
            "tool": data.get("tool"),
            "reason": data.get("reason"),
        }))
        return out

    if etype == T.BUDGET_EXCEEDED:
        out.append(mk("budget_exceeded", {"reason": data.get("reason")}))
        return out

    if etype == T.TURN_END:
        out.append(mk("turn_end", {
            "input_tokens": data.get("inputTokens"),
            "output_tokens": data.get("outputTokens"),
            "turns": data.get("turns"),
            "final_status": data.get("finalStatus"),
        }))
        return out

    if etype == T.SESSION_END:
        out.append(mk("session_end", {
            "final_status": data.get("finalStatus") or data.get("final_status"),
        }))
        return out

    if etype == T.COMPACTION:
        out.append(mk("compaction", {
            "summary": data.get("summary"),
            "kind": data.get("kind"),
            "message_count_before": data.get("messageCountBefore") or data.get("tokensBefore"),
            "message_count_after": data.get("messageCountAfter"),
        }))
        return out

    # 其它 entry 类型（leaf / label / session_info / session_start / model_change…）非投影事件。
    return out


def _branch_events(mgr: "SessionManager", agent_id: str) -> "list[TrajEvent]":
    """单 session 的全部 entry（写入序）→ 该 agent 的 TrajEvent 流。绝不抛。

    用 ``entries()``（写入序）而非 ``get_branch()``：B1 的派生遥测 entry（LLM_REQUEST / TURN_END /
    SESSION_END / PERMISSION_DECISION / TOOL_BLOCKED / BUDGET_EXCEEDED）是**注解型**——不推进 leaf，
    故不在对话 branch 上（leaf→root 链穿不过它们）。trajectory 要的是「实际发生了什么」的完整执行
    轨迹（对齐旧 append-only wire），而非「当前逻辑上下文」，故取写入序全量。

    branch/fork 还原（review medium）：据 conv-DAG（_assign_branches）给每条 conv entry 派生 branch_id +
    fork point，注解 entry 继承其 parent 的 branch；turn_id 按 TURN_END 边界递增。这恢复了 project.py 的
    per-(agent, branch) 隔离与 fork-point 链接（取代硬编码 "main"，使 in-file /fork、/clear、clone 后的
    step 谱系正确、被弃分支不再线性混入活分支）。
    """
    out: list[TrajEvent] = []
    try:
        all_entries = mgr.entries()
    except Exception:
        all_entries = []
    try:
        branch_of, branch_fork_point = _assign_branches(all_entries)
    except Exception:
        branch_of, branch_fork_point = {}, {}
    seqgen = _SeqGen()
    turn_n = [0]
    emitted_branches: set = set()
    for entry in all_entries:
        try:
            # branch_id：conv entry 取自身；注解 entry 继承其 parent（写时 leaf）的 branch。
            bid = branch_of.get(entry.id) or branch_of.get(entry.parentId) or "main"
            tid = f"turn_{agent_id}_{turn_n[0]}"
            evs = _emit_for_entry(entry, agent_id=agent_id, session_id=mgr.session_id,
                                  seqgen=seqgen, branch_id=bid, turn_id=tid)
            if evs and bid not in emitted_branches:
                emitted_branches.add(bid)
                fp = branch_fork_point.get(bid)
                if fp is not None:
                    evs[0].parent_event_id = fp     # 该 branch 首个 emitted event 链到 fork point
            out.extend(evs)
            if entry.type == T.TURN_END:
                turn_n[0] += 1                  # TURN_END 关闭当前 turn，后续 entry 归下一 turn
        except Exception:
            continue
    return out


def tree_events(session_id: str) -> "list[TrajEvent]":
    """把一个 canonical session（+ 其子会话 fan-out）重建为统一 TrajEvent 流。

    取代 ``events.reader.merge_session_events``：父会话 → agent_id="main"；每个子会话 →
    其 ``parent_session().agentId``（连字符，如 agent-001）。各 agent 内 seq 单调递增（branch 序），
    跨 agent 不承诺全序（与 wire 一致——配对在单 agent 内进行）。缺失会话 → 空流。绝不抛。
    """
    events: list[TrajEvent] = []
    try:
        if not session_file(session_id).exists():
            return events
        parent = SessionManager.open(session_id, lock=False)
    except Exception:
        return events

    events.extend(_branch_events(parent, _MAIN_AGENT))

    try:
        child_ids = children(session_id)
    except Exception:
        child_ids = []
    for child_sid in child_ids:
        try:
            child = SessionManager.open(child_sid, lock=False)
            ps = child.parent_session() or {}
            aid = ps.get("agentId") or child_sid
            events.extend(_branch_events(child, aid))
        except Exception:
            continue
    return events


# ─── listing / resolve（mirror reader.list_wire_sessions / resolve_wire_session）──────


def _header(session_id: str) -> "dict | None":
    """读 session.jsonl 首行 session_start（cheap listing）。缺/坏 → None。绝不抛。"""
    try:
        path = session_file(session_id)
        first = path.open(encoding="utf-8").readline().strip()
        d = json.loads(first) if first else {}
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("type") == T.SESSION_START else None


def _first_user_text(mgr: "SessionManager") -> str:
    """首条 user MESSAGE 文本（列表展示用）。绝不抛。"""
    try:
        for e in mgr.entries():
            if e.type != T.MESSAGE:
                continue
            msg = (e.data or {}).get("message")
            if isinstance(msg, dict) and msg.get("role") == "user":
                return _as_text(msg.get("content"))
    except Exception:
        return ""
    return ""


def list_tree_sessions() -> "list[dict]":
    """列出 ``sessions_dir()/*/session.jsonl`` 的**顶层**会话（排除带 parentSession 的子会话），
    按 mtime 倒序。shape：session_id / start_ts / first_user_msg / n_agents / n_events / mtime。绝不抛。"""
    root = sessions_dir()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for sdir in sorted(root.iterdir(), key=lambda p: p.name):
        if not sdir.is_dir():
            continue
        sid = sdir.name
        hdr = _header(sid)
        if hdr is None:
            continue
        # 子会话（header 带 parentSession）不作为顶层 trajectory 列出。
        if (hdr.get("data") or {}).get("parentSession"):
            continue
        try:
            mgr = SessionManager.open(sid, lock=False)
        except Exception:
            continue
        n_events = len(mgr.entries())
        child_ids = []
        try:
            child_ids = children(sid)
        except Exception:
            child_ids = []
        try:
            mtime = session_file(sid).stat().st_mtime
        except Exception:
            mtime = 0.0
        out.append({
            "session_id": sid,
            "n_events": n_events,
            "n_agents": 1 + len(child_ids),
            "mtime": mtime,
            "model": "",
            "start_ts": hdr.get("timestamp", ""),
            "first_user_msg": _first_user_text(mgr),
        })
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


def resolve_tree_session(arg: str) -> str:
    """``arg='latest'`` → 最新；否则按 session_id 前缀匹配。

    0 命中 → FileNotFoundError；多命中 → ValueError（mirror reader.resolve_wire_session）。
    """
    sessions = list_tree_sessions()
    if not sessions:
        raise FileNotFoundError("no tree sessions found under ~/.nanocode/sessions/")
    if arg == "latest":
        return sessions[0]["session_id"]  # 已按 mtime 倒序
    matches = [s["session_id"] for s in sessions if s["session_id"].startswith(arg)]
    if not matches:
        raise FileNotFoundError(f"no tree session matching '{arg}'")
    if len(matches) > 1:
        raise ValueError("ambiguous session id '" + arg + "': " + ", ".join(sorted(matches)))
    return matches[0]
