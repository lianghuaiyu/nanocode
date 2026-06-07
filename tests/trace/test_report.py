import json
import pytest
from nanocode.trace import report


def _write(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def test_load_events_skips_bad_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"type":"a"}\n\nNOT JSON\n{"type":"b"}\n', encoding="utf-8")
    ev = report.load_events(p)
    assert [e["type"] for e in ev] == ["a", "b"]


def test_list_sessions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".nanocode" / "traces" / "aaa11111.jsonl", [
        {"type": "session_start", "ts": "2026-01-01T00:00:00+00:00", "model": "step-3.7-flash"},
        {"type": "user_message", "text": "hello world"},
        {"type": "session_end", "input_tokens": 1000, "output_tokens": 200},
    ])
    _write(tmp_path / ".nanocode" / "traces" / "bbb22222.jsonl", [
        {"type": "session_start", "ts": "2026-02-02T00:00:00+00:00", "model": "x"},
        {"type": "user_message", "text": "second"},
    ])
    sessions = report.list_sessions()
    assert len(sessions) == 2
    ids = [s["session_id"] for s in sessions]
    assert set(ids) == {"aaa11111", "bbb22222"}
    a = next(s for s in sessions if s["session_id"] == "aaa11111")
    assert a["n_events"] == 3
    assert a["model"] == "step-3.7-flash"
    assert a["first_user_msg"] == "hello world"
    assert a["cost_usd"] > 0


def test_resolve_session_prefix_latest_ambiguous(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / ".nanocode" / "traces"
    _write(d / "abc111.jsonl", [{"type": "session_start"}])
    _write(d / "abc222.jsonl", [{"type": "session_start"}])
    _write(d / "zzz999.jsonl", [{"type": "session_start"}])
    # 唯一前缀
    assert report.resolve_session("zzz").name == "zzz999.jsonl"
    # 多义
    with pytest.raises(ValueError):
        report.resolve_session("abc")
    # 无匹配
    with pytest.raises(FileNotFoundError):
        report.resolve_session("nope")
    # latest = mtime 最新（zzz999 最后写）
    assert report.resolve_session("latest").name == "zzz999.jsonl"


def test_render_session_list_empty():
    out = report.render_session_list([])
    assert "No traces" in out


def test_render_timeline_compact_and_nesting():
    events = [
        {"seq": 0, "type": "session_start", "session_id": "main", "parent_session_id": None,
         "model": "m", "permission_mode": "default"},
        {"seq": 1, "type": "user_message", "session_id": "main", "parent_session_id": None, "text": "do it"},
        {"seq": 2, "type": "tool_call", "session_id": "main", "parent_session_id": None,
         "tool": "agent", "input": {"type": "explore"}},
        {"seq": 0, "type": "session_start", "session_id": "kid", "parent_session_id": "main",
         "model": "m", "permission_mode": "bypassPermissions"},
        {"seq": 1, "type": "tool_result", "session_id": "kid", "parent_session_id": "main",
         "tool": "read_file", "chars": 1234, "result": "X" * 5000},
    ]
    out = report.render_timeline(events, full=False)
    lines = out.splitlines()
    assert "user_message" in out and "do it" in out
    assert "read_file" in out and "1234 chars" in out
    # 子 Agent 事件缩进（kid 的行以空格开头）
    kid_lines = [l for l in lines if "read_file" in l]
    assert kid_lines and kid_lines[0].startswith("  ")
    # 紧凑模式不展开 5000 字符的 result
    assert "X" * 5000 not in out


def test_render_timeline_full_expands_result():
    events = [{"seq": 0, "type": "tool_result", "session_id": "m", "parent_session_id": None,
               "tool": "read_file", "chars": 10, "result": "FULLRESULTBODY"}]
    out = report.render_timeline(events, full=True)
    assert "FULLRESULTBODY" in out


def test_render_summary():
    events = [
        {"type": "tool_call", "tool": "read_file"},
        {"type": "tool_call", "tool": "read_file"},
        {"type": "tool_call", "tool": "run_shell"},
        {"type": "permission_decision", "tool": "run_shell", "action": "deny"},
        {"type": "session_start", "session_id": "kid", "parent_session_id": "main"},
        {"type": "session_end", "input_tokens": 2000, "output_tokens": 500, "turns": 3,
         "ts": "2026-01-01T00:00:10+00:00"},
    ]
    events.insert(0, {"type": "session_start", "session_id": "main",
                      "parent_session_id": None, "ts": "2026-01-01T00:00:00+00:00"})
    out = report.render_summary(events)
    assert "read_file: 2" in out
    assert "run_shell: 1" in out
    assert "2000 in / 500 out" in out
    assert "turns:" in out and "3" in out
    assert "sub-agents:" in out and "1" in out
    assert "denied:" in out and "1" in out
