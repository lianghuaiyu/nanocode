"""SessionEvent：会话事件溯源的盘上 schema（Pi 风格 entry tree）。

这是对现有 trace/ wire 事件 dict 的**形式化超集**：复用 `v`/`seq`/`ts`/`session_id`/
`parent_session_id`，新增 envelope 树链接字段 `id`/`parent_id`/`turn_id`/`branch_id`/
`agent_id`/`parent_event_id`。

锁盘约定（docs/09「现有事件源对账」）：
- event id = f"evt_{agent_id}_{seq}"（确定性、会话内唯一、无 RNG）。
- 盘上保持 flat-additive：payload 仍是顶层扁平键，**不**收进嵌套 `data`
  （否则破坏 trace/report.py._SUMMARIZERS 的顶层读取）。`SessionEvent.data` 是
  读侧派生视图——把非 envelope 顶层键归集而成。
- legacy flat 行（升级前 wire，无 `id`）由 `from_wire` 容忍：按 (agent_id, seq)
  反推 id、参与审计展示，但 `is_legacy` 为真，调用方据此决定不参与 tree rebuild。

本模块为纯数据 + 解析 helper，不依赖 nanocode 其它子系统（可被 trace 反向 import）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 沿用 trace 的 SCHEMA_VERSION，勿重置（promote 而非另起 lane）。
SCHEMA_VERSION = 1

# envelope（信封）键：会话事件的结构字段。其余顶层键属于 payload，
# 由 reader 归集为 SessionEvent.data。`ts` 与 `timestamp` 均视为时间戳键。
ENVELOPE_KEYS = frozenset({
    "v",
    "id",
    "session_id",
    "parent_session_id",
    "agent_id",
    "branch_id",
    "type",
    "ts",
    "timestamp",
    "seq",
    "parent_id",
    "parent_event_id",
    "turn_id",
    "data",
})


def event_id(agent_id: str, seq: int) -> str:
    """确定性、会话内唯一、无 RNG 的事件 id：``evt_{agent_id}_{seq}``。

    因 seq 在 resume 时从既有 wire tail 续号（见 reader.next_seq_from_wire 与
    Tracer.start_seq），跨运行不重号；且 legacy 行虽未存 id，也可由 (agent_id, seq)
    反推（agent_id 由读侧从文件路径注入），故 resume 的 legacy→new 边界链接无缝。

    约束：``agent_id`` 必须不含分隔符 ``_``（现状 artifact_id 为 ``main`` / ``agent-NNN``，
    用连字符）。id 当作**不透明键**使用（相等比较 / parent 指针），含 ``_`` 不会立即出错，
    但会破坏未来任何按 ``_`` 反解析 id 的逻辑——勿引入带下划线的 agent_id。
    """
    return f"evt_{agent_id}_{seq}"


def is_legacy(d: dict) -> bool:
    """legacy flat 行 = 升级前 wire 事件：有 `type`/`seq`/`ts`，但缺 envelope `id`。"""
    return "id" not in d


@dataclass
class SessionEvent:
    """一条会话事件的逻辑视图。

    盘上是 flat-additive dict；本 dataclass 由 ``from_wire`` 解析得到，``data`` 为
    非 envelope 顶层键的派生归集，便于 reader/renderer 消费。
    """

    v: int
    id: str
    session_id: str
    agent_id: str
    branch_id: str
    type: str
    ts: str
    seq: int
    parent_id: str | None = None
    parent_event_id: str | None = None
    turn_id: str | None = None
    parent_session_id: str | None = None
    legacy: bool = False
    line_no: int = 0  # 读侧产物（文件内 0-based 行号），用于稳定 merge 排序；非盘上 schema
    data: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, d: dict, *, agent_id: str) -> "SessionEvent":
        """从一行 wire dict 解析为 SessionEvent。

        `agent_id` 由读侧从文件路径注入（``agents/<agent_id>/wire.jsonl``）——legacy
        行不含 `agent_id`，新行含但应与路径一致。legacy 行按 (agent_id, seq) 反推 id。
        payload = 非 envelope 顶层键归集。
        """
        seq = _as_int(d.get("seq"), 0)
        resolved_agent = d.get("agent_id") or agent_id
        ev_id = d.get("id") or event_id(resolved_agent, seq)
        data = d.get("data")
        if not isinstance(data, dict):
            data = {k: v for k, v in d.items() if k not in ENVELOPE_KEYS}
        return cls(
            v=_as_int(d.get("v"), SCHEMA_VERSION),
            id=ev_id,
            session_id=d.get("session_id", ""),
            agent_id=resolved_agent,
            branch_id=d.get("branch_id", "main"),
            type=d.get("type", ""),
            ts=d.get("ts") or d.get("timestamp", ""),
            seq=seq,
            parent_id=d.get("parent_id"),
            parent_event_id=d.get("parent_event_id"),
            turn_id=d.get("turn_id"),
            parent_session_id=d.get("parent_session_id"),
            legacy=is_legacy(d),
            data=data,
        )


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
