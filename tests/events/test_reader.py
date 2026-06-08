"""events.reader：续号、单 agent 读取、跨 agent 读时 merge（含 legacy / malformed 容忍）。"""

import json

from nanocode.session import v2
from nanocode.events.reader import (
    next_seq_from_wire,
    read_agent_wire,
    merge_session_events,
    session_agent_wires,
)


def _write_wire(session_id: str, agent_id: str, rows: list[dict]) -> "object":
    path = v2.agent_wire_path(session_id, agent_id)  # 建目录并返回路径
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_next_seq_missing_file_is_zero(tmp_path):
    assert next_seq_from_wire(tmp_path / "nope.jsonl") == 0


def test_next_seq_continues_from_tail():
    p = _write_wire("s1", "main", [
        {"seq": 0, "type": "a", "ts": "t"},
        {"seq": 1, "type": "b", "ts": "t"},
        {"seq": 2, "type": "c", "ts": "t"},
    ])
    assert next_seq_from_wire(p) == 3  # resume-safe：续到 max+1


def test_next_seq_robust_to_malformed_and_torn_tail():
    p = v2.agent_wire_path("s2", "main")
    p.write_text(
        json.dumps({"seq": 0, "type": "a", "ts": "t"}) + "\n"
        + "{ this is not json\n"                       # 中段坏行
        + json.dumps({"seq": 5, "type": "b", "ts": "t"}) + "\n"
        + '{"seq": 6, "type": "c"',                    # torn tail（无换行、半行）
        encoding="utf-8",
    )
    assert next_seq_from_wire(p) == 6  # 扫全文件取 max(0,5)+1；坏行/半行跳过


def test_read_agent_wire_injects_agent_id_and_line_no():
    p = _write_wire("s3", "agent-001", [
        {"seq": 0, "type": "user_message", "ts": "t", "text": "hi"},      # legacy
        {"id": "evt_agent-001_1", "seq": 1, "type": "turn_end", "ts": "t",
         "agent_id": "agent-001", "parent_id": "evt_agent-001_0"},        # new
    ])
    evs = read_agent_wire(p, "agent-001")
    assert [e.line_no for e in evs] == [0, 1]
    assert evs[0].legacy is True and evs[0].id == "evt_agent-001_0"
    assert evs[1].legacy is False and evs[1].parent_id == "evt_agent-001_0"
    assert all(e.agent_id == "agent-001" for e in evs)


def test_merge_orders_by_ts_agent_seq_lineno_and_includes_legacy():
    # main：两条；agent-001：一条，ts 居中
    _write_wire("s4", "main", [
        {"seq": 0, "type": "user_message", "ts": "2026-06-08T10:00:00Z", "text": "q"},  # legacy
        {"id": "evt_main_1", "agent_id": "main", "seq": 1, "type": "turn_end",
         "ts": "2026-06-08T10:00:05Z", "parent_id": "evt_main_0"},
    ])
    _write_wire("s4", "agent-001", [
        {"id": "evt_agent-001_0", "agent_id": "agent-001", "seq": 0, "type": "user_message",
         "ts": "2026-06-08T10:00:02Z", "text": "sub"},
    ])
    merged = merge_session_events("s4")
    # 展示序按 ts：10:00:00(main) < 10:00:02(agent-001) < 10:00:05(main)
    assert [(e.agent_id, e.seq) for e in merged] == [("main", 0), ("agent-001", 0), ("main", 1)]
    # legacy 行参与展示
    assert merged[0].legacy is True and merged[0].id == "evt_main_0"


def test_merge_same_ts_tiebreaks_by_agent_then_seq():
    ts = "2026-06-08T10:00:00Z"
    _write_wire("s5", "main", [
        {"id": "evt_main_0", "agent_id": "main", "seq": 0, "type": "a", "ts": ts},
        {"id": "evt_main_1", "agent_id": "main", "seq": 1, "type": "b", "ts": ts},
    ])
    _write_wire("s5", "agent-001", [
        {"id": "evt_agent-001_0", "agent_id": "agent-001", "seq": 0, "type": "c", "ts": ts},
    ])
    merged = merge_session_events("s5")
    # 同 ts → 按 agent_id 再 seq："agent-001" < "main"
    assert [(e.agent_id, e.seq) for e in merged] == [
        ("agent-001", 0), ("main", 0), ("main", 1),
    ]


def test_merge_empty_session_is_empty():
    assert merge_session_events("does-not-exist") == []
    assert session_agent_wires("does-not-exist") == []
