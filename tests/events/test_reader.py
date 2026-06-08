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
    # 用真实 emitter 的 ts 形态（UTC isoformat，+00:00 微秒），而非 Z，以真正校验字符串序契约
    # main：两条；agent-001：一条，ts 居中
    _write_wire("s4", "main", [
        {"seq": 0, "type": "user_message", "ts": "2026-06-08T10:00:00.000000+00:00", "text": "q"},  # legacy
        {"id": "evt_main_1", "agent_id": "main", "seq": 1, "type": "turn_end",
         "ts": "2026-06-08T10:00:05.000000+00:00", "parent_id": "evt_main_0"},
    ])
    _write_wire("s4", "agent-001", [
        {"id": "evt_agent-001_0", "agent_id": "agent-001", "seq": 0, "type": "user_message",
         "ts": "2026-06-08T10:00:02.000000+00:00", "text": "sub"},
    ])
    merged = merge_session_events("s4")
    # 展示序按 ts：10:00:00(main) < 10:00:02(agent-001) < 10:00:05(main)
    assert [(e.agent_id, e.seq) for e in merged] == [("main", 0), ("agent-001", 0), ("main", 1)]
    # legacy 行参与展示
    assert merged[0].legacy is True and merged[0].id == "evt_main_0"


def test_merge_same_ts_tiebreaks_by_agent_then_seq():
    ts = "2026-06-08T10:00:00.000000+00:00"
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


def test_torn_tail_then_append_does_not_corrupt_first_resumed_event():
    """Codex P2 回归：上轮崩溃留下半行 JSON，resume 续写不得把首个新事件粘成不可解析合并行。

    JsonlSink._ensure_open 现在在 append 前对无尾换行的文件补一个换行：残缺半行独占一行
    （读侧跳过），新事件从干净行开始，parent_id 链不悬空。
    """
    from nanocode.trace.sinks import JsonlSink
    from nanocode.trace.tracer import Tracer

    p = v2.agent_wire_path("torn", "main")
    # 一条完好行 + 一条 torn tail（无换行、半行 JSON）——模拟崩溃
    p.write_text(
        json.dumps({"v": 1, "id": "evt_main_0", "agent_id": "main", "seq": 0,
                    "type": "user_message", "ts": "2026-06-08T10:00:00.000000+00:00"}) + "\n"
        + '{"seq": 1, "type": "broken"',
        encoding="utf-8",
    )
    # resume：从 tail 续号 + 真实 JsonlSink append
    start = next_seq_from_wire(p)            # 半行不计 → 1
    t = Tracer("torn", [JsonlSink(p)], agent_id="main", start_seq=start)
    t.begin_turn()
    t.emit("user_message", text="resumed")  # seq 1
    t.emit("turn_end")                       # seq 2
    t.close()

    evs = read_agent_wire(p, "main")
    parseable_ids = {e.id for e in evs}
    # 首个完好事件 + 两个 resume 事件都可解析（半行被跳过，不污染下一行）
    assert [e.type for e in evs] == ["user_message", "user_message", "turn_end"]
    assert [e.seq for e in evs] == [0, 1, 2]
    # 关键：没有任何事件的 parent_id 指向一个不存在的 id（链不悬空）
    for e in evs:
        if e.parent_id is not None:
            assert e.parent_id in parseable_ids, f"dangling parent_id {e.parent_id}"
    # resume 首事件链到上轮 tail 的完好事件
    assert evs[1].parent_id == "evt_main_0"
