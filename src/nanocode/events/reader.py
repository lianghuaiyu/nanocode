"""读侧：读取 per-agent wire 事件、续号、跨 agent 读时 merge。

锁定决策（docs/09「现有事件源对账」）：
- 不新增 session 根文件；跨 agent 统一时间线由 `merge_session_events` 读时合成。
- 审计展示序 = (ts, agent_id, seq, line_no)；因果序只看 parent_id 且只在单 agent 内成立，
  跨兄弟 agent 不承诺全序。
- legacy flat 行（无 envelope id）参与审计展示、不参与 tree rebuild（`SessionEvent.legacy`）。
- malformed 行容忍跳过：审计读侧不因坏行整体失败（事实源的 torn-tail/中段坏行处理由
  写侧/未来 event_store 负责，本 reader 只做审计读取的健壮性）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..paths import sessions_dir
from .models import SessionEvent, _as_int


def _iter_json_lines(path: Path):
    """逐行 yield (line_no, dict)；跳过空行与 malformed 行，绝不抛出。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue  # malformed 行：审计读侧跳过
        if isinstance(obj, dict):
            yield i, obj


def next_seq_from_wire(path: "Path | str") -> int:
    """返回该 wire 文件的 ``max(seq) + 1``（resume-safe 续号）；不存在/空/全坏则 0。

    `wire.jsonl` 跨 resume append，而 ``Tracer._seq`` 每次构造重置为 0；若不从 tail
    续号，``evt_{agent_id}_{seq}`` 会与上一轮 id 碰撞（见 docs/09 决定 8）。扫描全文件
    取 max（而非仅末行），以对 torn-tail 健壮。
    """
    p = Path(path)
    max_seq = -1
    for _, obj in _iter_json_lines(p):
        if "seq" in obj:
            max_seq = max(max_seq, _as_int(obj.get("seq"), -1))
    return max_seq + 1


def read_agent_wire(path: "Path | str", agent_id: str) -> list[SessionEvent]:
    """读取单个 agent 的 wire，返回 SessionEvent 列表（带 line_no，保文件内顺序）。

    `agent_id` 由调用方从路径注入（``agents/<agent_id>/wire.jsonl``）——legacy 行不含
    `agent_id`，据此反推其 id。
    """
    out: list[SessionEvent] = []
    for line_no, obj in _iter_json_lines(Path(path)):
        ev = SessionEvent.from_wire(obj, agent_id=agent_id)
        ev.line_no = line_no
        out.append(ev)
    return out


def _agent_id_from_wire_path(wire_path: Path) -> str:
    """``.../agents/<agent_id>/wire.jsonl`` -> ``<agent_id>``。"""
    return wire_path.parent.name


def session_agent_wires(session_id: str) -> list[Path]:
    """列出某 session 下所有 ``agents/*/wire.jsonl``（按 agent_id 排序，稳定）。"""
    agents_root = sessions_dir() / session_id / "agents"
    if not agents_root.is_dir():
        return []
    return sorted(
        (d / "wire.jsonl" for d in agents_root.iterdir() if d.is_dir()),
        key=lambda p: p.parent.name,
    )


def merge_session_events(session_id: str) -> list[SessionEvent]:
    """跨 agent 读时 merge：合并某 session 下所有 per-agent wire 为统一审计时间线。

    展示序 = (ts, agent_id, seq, line_no)，稳定、可复现。这是**展示序、非因果序**——
    跨兄弟 agent 不承诺全序；因果链只在单 agent 内由 `parent_id` 保证。

    注意：`ts` 按**字符串**排序，等价于时间序仅因唯一写者 `Tracer.emit` 恒用
    `datetime.now(timezone.utc).isoformat()`（定宽 `+00:00`）。若将来引入第二个/外部写者，
    或 legacy 行带非 UTC 偏移（如 `+08:00`）或 `Z` 后缀的 `timestamp`，字符串序会与时间序
    背离（仅影响展示，不影响 parent_id 因果）——届时应在此改为解析为 UTC datetime 再排序。
    """
    events: list[SessionEvent] = []
    for wire in session_agent_wires(session_id):
        events.extend(read_agent_wire(wire, _agent_id_from_wire_path(wire)))
    events.sort(key=lambda e: (e.ts, e.agent_id, e.seq, e.line_no))
    return events