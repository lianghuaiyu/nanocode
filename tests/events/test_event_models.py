"""events.models：SessionEvent schema + 解析 helper（含 legacy 容忍）。"""

from nanocode.events.models import (
    SCHEMA_VERSION,
    ENVELOPE_KEYS,
    SessionEvent,
    event_id,
    is_legacy,
)


def test_event_id_deterministic_no_rng():
    assert event_id("main", 0) == "evt_main_0"
    assert event_id("agent-001", 42) == "evt_agent-001_42"
    # 确定性：同输入恒同输出
    assert event_id("main", 7) == event_id("main", 7)


def test_is_legacy_detects_missing_envelope_id():
    assert is_legacy({"type": "tool_call", "seq": 3, "ts": "t"}) is True
    assert is_legacy({"id": "evt_main_3", "type": "tool_call", "seq": 3}) is False


def test_from_wire_legacy_synthesizes_id_and_collects_payload():
    # 升级前 wire 行：无 id/parent_id，payload 在顶层
    d = {"v": 1, "ts": "2026-06-08T10:00:00Z", "session_id": "s", "seq": 3,
         "type": "tool_call", "tool": "grep_search", "input": {"pattern": "E"},
         "tool_use_id": "tu_1"}
    ev = SessionEvent.from_wire(d, agent_id="main")
    assert ev.legacy is True
    assert ev.id == "evt_main_3"          # 按 (agent_id, seq) 反推
    assert ev.agent_id == "main"          # 由路径注入
    assert ev.parent_id is None
    assert ev.branch_id == "main"
    # payload 归集为 data（非 envelope 顶层键）
    assert ev.data == {"tool": "grep_search", "input": {"pattern": "E"}, "tool_use_id": "tu_1"}


def test_from_wire_new_preserves_envelope_and_links():
    d = {"v": 1, "ts": "t", "session_id": "s", "seq": 7, "type": "user_message",
         "id": "evt_main_7", "agent_id": "main", "branch_id": "main",
         "parent_id": "evt_main_6", "turn_id": "turn_2", "text": "hi"}
    ev = SessionEvent.from_wire(d, agent_id="main")
    assert ev.legacy is False
    assert ev.id == "evt_main_7"
    assert ev.parent_id == "evt_main_6"
    assert ev.turn_id == "turn_2"
    assert ev.data == {"text": "hi"}


def test_from_wire_accepts_explicit_nested_data():
    # 未来 schema 若用嵌套 data，from_wire 直接采用，不再归集顶层
    d = {"id": "x", "type": "t", "seq": 0, "ts": "t", "data": {"role": "assistant"}}
    ev = SessionEvent.from_wire(d, agent_id="main")
    assert ev.data == {"role": "assistant"}


def test_from_wire_ts_falls_back_to_timestamp_key():
    ev = SessionEvent.from_wire({"id": "x", "type": "t", "seq": 0, "timestamp": "T"}, agent_id="main")
    assert ev.ts == "T"


def test_agent_id_from_wire_field_wins_over_path():
    # 新行自带 agent_id 时以其为准（应与路径一致；防御性）
    ev = SessionEvent.from_wire({"id": "e", "type": "t", "seq": 1, "agent_id": "agent-002"}, agent_id="main")
    assert ev.agent_id == "agent-002"
    assert "agent_id" in ENVELOPE_KEYS and SCHEMA_VERSION == 1
