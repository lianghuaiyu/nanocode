"""session/tree.py — canonical 会话树：envelope + entry union + 中立 Message + 纯函数。

Pi 风格（earendil-works/pi @ 9ccfcd7）移植，见 docs/13。**刻意与 wire 的 `agent_id+seq`
id（events/models.py）解绑**：用 uuidv7 式时间有序 id，使 clone/import 自由、行序≠树。

本模块只含纯数据 + 纯函数（同输入同输出），**不做任何 I/O**（I/O 在 manager.py）。
统一原则：存全事实；能否发回 provider 由 render.py 严格 gate（见 docs/13 §4）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

V = 1  # envelope schema 版本

# ─── id 生成：uuidv7 式、时间有序、进程安全、全长（docs/13 §3.1） ──────────────
# Python 3.14 stdlib 有 uuid.uuid7()；3.11–3.13 无 → 回退到「毫秒时间前缀 + uuid4 尾」，
# 同样时间有序且跨进程唯一（共享 worktree 多进程）。全长 id 免去 Pi 8 字符截断的撞 id 重试。
_HAS_UUID7 = hasattr(uuid, "uuid7")


def new_id(prefix: str = "ent") -> str:
    if _HAS_UUID7:  # pragma: no cover - 取决于运行时 Python 版本
        return f"{prefix}_{uuid.uuid7().hex}"
    return f"{prefix}_{int(time.time() * 1000):012x}{uuid.uuid4().hex[:16]}"


def now_iso() -> str:
    """UTC ISO8601（秒级 Z 后缀），与 wire ts 风格一致。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ─── entry 类型常量（docs/13 §3.2 entry union） ──────────────────────────────
SESSION_START = "session_start"
MESSAGE = "message"
CUSTOM_MESSAGE = "custom_message"
CUSTOM = "custom"
COMPACTION = "compaction"
BRANCH_SUMMARY = "branch_summary"
MODEL_CHANGE = "model_change"
THINKING_LEVEL_CHANGE = "thinking_level_change"
ACTIVE_TOOLS_CHANGE = "active_tools_change"
LEAF = "leaf"
LABEL = "label"
SESSION_INFO = "session_info"
PERMISSION_DECISION = "permission_decision"
TASK_UPDATE = "task_update"
SESSION_END = "session_end"
# docs/14 Milestone B：把原本只进 wire 的派生遥测落进 canonical 树（trajectory 从树派生、不再读 wire）。
# 这些都是**注解型** entry——不入 FOLD_TYPES（对 LLM 不可见）、不推进 leaf（见 leaf_id_after_entry）。
TOOL_BLOCKED = "tool_blocked"
BUDGET_EXCEEDED = "budget_exceeded"
TURN_END = "turn_end"
LLM_REQUEST = "llm_request"

# fold 进上下文的 entry（消息族 + 派生上下文）。设置类只参与标量 fold；其余对 LLM 不可见。
FOLD_TYPES = frozenset({MESSAGE, CUSTOM_MESSAGE, COMPACTION, BRANCH_SUMMARY})


@dataclass
class Entry:
    """会话树的一条 entry。盘上是一行 JSON；树关系只看 ``parentId``，行序仅写入序。"""

    type: str
    id: str
    parentId: str | None
    sessionId: str
    timestamp: str
    data: dict = field(default_factory=dict)
    v: int = V

    def to_dict(self) -> dict:
        return {
            "v": self.v,
            "id": self.id,
            "parentId": self.parentId,
            "sessionId": self.sessionId,
            "type": self.type,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        return cls(
            type=d.get("type", ""),
            id=d["id"],
            parentId=d.get("parentId"),
            sessionId=d.get("sessionId", ""),
            timestamp=d.get("timestamp", ""),
            data=d.get("data") if isinstance(d.get("data"), dict) else {},
            v=int(d.get("v", V)),
        )


# ─── leaf 折叠规则（Pi leafIdAfterEntry，docs/13 §4.3） ───────────────────────
class _Unchanged:
    """哨兵：该 entry 不移动 leaf（label / session_info 只标注、不推进对话）。"""


_UNCHANGED = _Unchanged()


def leaf_id_after_entry(e: Entry) -> Any:
    """统一规则：``leaf`` entry → 其 ``targetId``（可为 None=重置到 root）；
    ``session_start``/``label``/``session_info`` 及派生遥测（permission_decision/tool_blocked/
    budget_exceeded/turn_end/session_end/llm_request）→ 不变（哨兵）；其余 → 自身 id。

    session_start 是 **header / metadata**，遥测是 **注解**——都不推进对话 branch：不移动 leaf，
    使后续消息的 parentId 链不穿过它们、build_context branch 干净（docs/14 §4.1 / Milestone B）。"""
    if e.type == LEAF:
        return e.data.get("targetId")
    if e.type in (SESSION_START, LABEL, SESSION_INFO,
                  PERMISSION_DECISION, TOOL_BLOCKED, BUDGET_EXCEEDED, TURN_END, SESSION_END, LLM_REQUEST):
        return _UNCHANGED
    return e.id


def current_leaf(entries: list[Entry]) -> str | None:
    """把 ``leaf_id_after_entry`` 折叠到最后一条 leaf-affecting entry（末行胜出）。

    entries 按写入序（盘上行序）。空 → None。leaf entry 的 targetId=None 把 leaf 复位到 root。
    """
    leaf: str | None = None
    for e in entries:
        r = leaf_id_after_entry(e)
        if r is _UNCHANGED:
            continue
        leaf = r
    return leaf


# ─── tree 遍历（docs/13 §4：getBranch leaf→root，反转 root-first） ────────────
class SessionTreeError(Exception):
    pass


class SessionBusyError(Exception):
    """另一进程已持有该 session 的写锁（docs/14 §4.6/§6a + SessionLease）。

    写者身份归 runtime active-thread 的 `SessionLease`（startup / rebind / 子 agent spawn 都经
    lease 以 lock=True 打开或创建）。`Agent.__init__` 不再取锁——构造模型 core 不等于占用 writer。
    同进程不会对同一 sid 取两把锁（clone/fork 的 child create 用 lock=False，交给 lease 重开），
    故无单进程自锁死。进程死亡时 flock 由内核自动释放（advisory），无需手工 stale 检测。"""
    pass


def index_by_id(entries: list[Entry]) -> dict[str, Entry]:
    return {e.id: e for e in entries}


def get_branch(by_id: dict[str, Entry], leaf_id: str | None) -> list[Entry]:
    """从 leaf 沿 parentId 回溯到 root，返回 **root-first**。O(branch depth)。

    leaf_id=None → []（空上下文）。链断裂抛 SessionTreeError（不静默截断）。
    """
    if leaf_id is None:
        return []
    cur = by_id.get(leaf_id)
    if cur is None:
        raise SessionTreeError(f"leaf {leaf_id} not found")
    path: list[Entry] = []
    seen: set[str] = set()
    while cur is not None:
        if cur.id in seen:
            raise SessionTreeError(f"cycle at {cur.id}")
        seen.add(cur.id)
        path.append(cur)
        if not cur.parentId:
            break
        parent = by_id.get(cur.parentId)
        if parent is None:
            raise SessionTreeError(f"parent {cur.parentId} of {cur.id} not found")
        cur = parent
    path.reverse()
    return path


# ─── 元数据折叠（LWW + tombstone，docs/13 §5 / Pi session.ts） ────────────────
def labels_by_id(entries: list[Entry]) -> dict[str, str]:
    """label entry 的 last-write-wins 折叠；空/空白 label = tombstone（删除）。"""
    out: dict[str, str] = {}
    for e in entries:
        if e.type != LABEL:
            continue
        target = e.data.get("targetId")
        if target is None:
            continue
        label = (e.data.get("label") or "").strip()
        if label:
            out[target] = label
        else:
            out.pop(target, None)
    return out


def session_name(entries: list[Entry]) -> str | None:
    """末个 session_info 胜出；其 name 为空则清空（tombstone，对齐 Pi getSessionName）。"""
    name: str | None = None
    for e in entries:
        if e.type == SESSION_INFO:
            n = (e.data.get("name") or "").strip()
            name = n or None
    return name


# ─── 中立 Message 构造器（docs/13 §3.3，抄 Pi packages/ai/src/types.ts） ───────
# 表示为 JSON 原生 dict（存于 entry.data["message"]，由 render.py 渲染成 provider 形状）。
# role: user | assistant | toolResult；content block 以 "type" 判别。


def text_block(text: str, *, signature: str | None = None) -> dict:
    b = {"type": "text", "text": text}
    if signature is not None:
        b["textSignature"] = signature
    return b


def thinking_block(thinking: str, *, signature: str | None = None, redacted: bool = False) -> dict:
    b: dict = {"type": "thinking", "thinking": thinking}
    if signature is not None:
        b["thinkingSignature"] = signature
    if redacted:
        b["redacted"] = True
    return b


def image_block(data: str, mime_type: str) -> dict:
    return {"type": "image", "data": data, "mimeType": mime_type}


def tool_call_block(id: str, name: str, arguments: dict, *, thought_signature: str | None = None) -> dict:
    b = {"type": "toolCall", "id": id, "name": name, "arguments": arguments}
    if thought_signature is not None:
        b["thoughtSignature"] = thought_signature
    return b


def user_message(content: str | list[dict], *, timestamp: str | None = None) -> dict:
    return {"role": "user", "content": content, "timestamp": timestamp or now_iso()}


def assistant_message(
    content: list[dict],
    *,
    provider: str,
    api: str,
    model: str,
    stop_reason: str,
    usage: dict | None = None,
    latency_ms: int | None = None,
    timestamp: str | None = None,
) -> dict:
    m = {
        "role": "assistant",
        "content": content,
        "provider": provider,
        "api": api,
        "model": model,
        "stopReason": stop_reason,
        "timestamp": timestamp or now_iso(),
    }
    # docs/14 Milestone B：per-call 遥测落在消息上（trajectory 派生用）。render.py 只读
    # role/content/provider/... 的固定字段，不读 usage/latencyMs → 绝不发回 provider（对 LLM 不可见）。
    if usage is not None:
        m["usage"] = usage
    if latency_ms is not None:
        m["latencyMs"] = latency_ms
    return m


def tool_result_message(
    *,
    tool_call_id: str,
    tool_name: str,
    content: list[dict] | str,
    is_error: bool = False,
    details: Any | None = None,
    latency_ms: int | None = None,
    timestamp: str | None = None,
) -> dict:
    m = {
        "role": "toolResult",
        "toolCallId": tool_call_id,
        "toolName": tool_name,
        "content": content,
        "isError": is_error,
        "timestamp": timestamp or now_iso(),
    }
    if details is not None:
        m["details"] = details
    if latency_ms is not None:
        m["latencyMs"] = latency_ms       # docs/14 Milestone B：per-tool 延迟（render 忽略，trajectory 派生用）
    return m
