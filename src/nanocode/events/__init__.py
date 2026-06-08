"""事件溯源内核（spine）。

本包是 nanocode 「事件化内核」的第一块地基：把每个 agent always-on 的
`agents/<id>/wire.jsonl` 形式化为 Pi 风格的 entry tree（盘上事实源），并提供
读时 merge/projection 生成跨 agent 的统一审计时间线。

设计约定（详见 docs/09 「现有事件源对账」锁定决策）：
- 事实源 = per-agent `wire.jsonl`，原地升级（写侧由 trace.Tracer enrich，见 trace/tracer.py）。
- 不新增 session 根 events.jsonl；统一流由 `reader.merge_session_events` 读时合成。
- event id = f"evt_{agent_id}_{seq}"，确定性、会话内唯一、无 RNG。
- seq 在 resume 时从 wire tail/max 续号，避免跨运行 id 碰撞（见 reader.next_seq_from_wire）。
- 盘上保持 flat-additive：只加 envelope 字段，payload 仍扁平（不破坏 trace/report.py）。
"""

from .models import (
    SCHEMA_VERSION,
    ENVELOPE_KEYS,
    SessionEvent,
    event_id,
    is_legacy,
)
from .reader import (
    next_seq_from_wire,
    read_agent_wire,
    merge_session_events,
)

__all__ = [
    "SCHEMA_VERSION",
    "ENVELOPE_KEYS",
    "SessionEvent",
    "event_id",
    "is_legacy",
    "next_seq_from_wire",
    "read_agent_wire",
    "merge_session_events",
]
