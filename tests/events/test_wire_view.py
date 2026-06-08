"""`nanocode trace --wire`：wire-lane 列表/解析 + 时间线/汇总渲染。"""

import json

from nanocode.session import v2
from nanocode.events import reader
from nanocode.trace import report
from nanocode.entrypoints import trace_cmd


def _write_wire(session_id: str, agent_id: str, rows: list[dict]):
    path = v2.agent_wire_path(session_id, agent_id)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _seed(session_id="sess-aaa"):
    _write_wire(session_id, "main", [
        {"v": 1, "id": "evt_main_0", "agent_id": "main", "seq": 0, "type": "session_start",
         "ts": "2026-06-08T10:00:00.000000+00:00", "model": "claude-x", "permission_mode": "default"},
        {"v": 1, "id": "evt_main_1", "agent_id": "main", "seq": 1, "type": "user_message",
         "ts": "2026-06-08T10:00:01.000000+00:00", "parent_id": "evt_main_0", "text": "fix CI"},
        {"v": 1, "id": "evt_main_2", "agent_id": "main", "seq": 2, "type": "tool_call",
         "ts": "2026-06-08T10:00:02.000000+00:00", "parent_id": "evt_main_1",
         "tool": "grep_search", "input": {"pattern": "ERROR"}, "tool_use_id": "tu_1"},
        {"v": 1, "id": "evt_main_3", "agent_id": "main", "seq": 3, "type": "turn_end",
         "ts": "2026-06-08T10:00:06.000000+00:00", "parent_id": "evt_main_2",
         "input_tokens": 100, "output_tokens": 20, "turns": 1},
    ])
    _write_wire(session_id, "agent-001", [
        {"v": 1, "id": "evt_agent-001_0", "agent_id": "agent-001", "seq": 0, "type": "user_message",
         "ts": "2026-06-08T10:00:03.000000+00:00", "text": "sub task"},
        {"v": 1, "id": "evt_agent-001_1", "agent_id": "agent-001", "seq": 1, "type": "tool_call",
         "ts": "2026-06-08T10:00:04.000000+00:00", "parent_id": "evt_agent-001_0",
         "tool": "read_file", "input": {"file_path": "x.py"}, "tool_use_id": "tu_2"},
    ])
    return session_id


def test_list_wire_sessions_reports_agents_and_first_msg():
    _seed("sess-aaa")
    sessions = reader.list_wire_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "sess-aaa"
    assert s["n_agents"] == 2 and s["n_events"] == 6
    assert s["model"] == "claude-x"
    assert s["first_user_msg"] == "fix CI"


def test_resolve_wire_session_prefix_latest_and_errors():
    _seed("sess-aaa")
    assert reader.resolve_wire_session("sess-") == "sess-aaa"
    assert reader.resolve_wire_session("latest") == "sess-aaa"
    try:
        reader.resolve_wire_session("nope")
        assert False
    except FileNotFoundError:
        pass


def test_resolve_wire_ambiguous_raises():
    _write_wire("sess-aaa", "main", [{"id": "evt_main_0", "agent_id": "main", "seq": 0, "type": "x", "ts": "t"}])
    _write_wire("sess-abb", "main", [{"id": "evt_main_0", "agent_id": "main", "seq": 0, "type": "x", "ts": "t"}])
    try:
        reader.resolve_wire_session("sess-a")
        assert False
    except ValueError:
        pass


def test_render_wire_timeline_merges_agents_in_ts_order():
    _seed("sess-aaa")
    events = reader.merge_session_events("sess-aaa")
    out = report.render_wire_timeline(events)
    lines = out.splitlines()
    assert lines[0].startswith("AGENT")
    # 合并后按 ts：main(0,1,2) 然后 agent-001(0,1) 然后 main turn_end(3)
    body = [l for l in lines[1:]]
    assert "main" in body[0] and "session_start" in body[0]
    assert "agent-001" in [l.split()[0] for l in body]  # 子 agent 行出现
    # tool_call 详情体被复用渲染
    assert any("grep_search" in l for l in body)
    assert any("read_file" in l for l in body)
    # 时间线整体按 ts：agent-001 的两行应排在 main turn_end(10:00:06) 之前
    types_in_order = [l.split()[2] if len(l.split()) > 2 else "" for l in body]
    assert types_in_order[-1] == "turn_end"


def test_render_wire_summary_counts_agents_and_tools():
    _seed("sess-aaa")
    events = reader.merge_session_events("sess-aaa")
    out = report.render_wire_summary(events)
    assert "events:      6" in out
    assert "agents:      2 (1 sub)" in out      # main + 1 sub
    assert "turns:       1" in out
    assert "100 in / 20 out" in out
    assert "grep_search: 1" in out and "read_file: 1" in out


def test_render_wire_summary_flags_legacy_rows():
    # legacy 行（无 id）参与展示并被计数
    _write_wire("sess-leg", "main", [
        {"seq": 0, "type": "user_message", "ts": "2026-06-08T10:00:00.000000+00:00", "text": "old"},
        {"id": "evt_main_1", "agent_id": "main", "seq": 1, "type": "turn_end",
         "ts": "2026-06-08T10:00:01.000000+00:00", "input_tokens": 5, "output_tokens": 2, "turns": 1},
    ])
    events = reader.merge_session_events("sess-leg")
    out = report.render_wire_summary(events)
    assert "(1 legacy)" in out


def test_trace_cmd_wire_list_and_timeline_smoke(capsys):
    _seed("sess-aaa")
    assert trace_cmd.run(["--wire"]) == 0           # 列表
    assert "sess-aaa" in capsys.readouterr().out
    assert trace_cmd.run(["--wire", "sess-aaa"]) == 0  # 时间线
    out = capsys.readouterr().out
    assert "grep_search" in out and "agent-001" in out
    assert trace_cmd.run(["--wire", "sess-aaa", "--summary"]) == 0
    assert "agents:" in capsys.readouterr().out


def test_trace_cmd_wire_unknown_session_errors():
    _seed("sess-aaa")
    assert trace_cmd.run(["--wire", "zzz"]) == 1
