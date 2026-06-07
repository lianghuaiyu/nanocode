from pathlib import Path
from nanocode.trace.report import load_session_events, render_timeline


def _write(p: Path, *lines):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_merges_sandbox_children(tmp_path):
    main = tmp_path / "ABC.jsonl"
    _write(main, '{"seq":0,"type":"user_message","session_id":"ABC"}')
    child = tmp_path / "ABC" / "sandbox" / "eph-1" / "c.jsonl"
    _write(child, '{"seq":0,"type":"tool_call","session_id":"kid","parent_session_id":"ABC","tool":"run_shell"}')
    events = load_session_events(main)
    assert len(events) == 2
    assert any(e.get("parent_session_id") == "ABC" for e in events)


def test_no_children_dir_is_fine(tmp_path):
    main = tmp_path / "SOLO.jsonl"
    _write(main, '{"seq":0,"type":"user_message","session_id":"SOLO"}')
    events = load_session_events(main)
    assert len(events) == 1


def test_child_event_indented_in_timeline(tmp_path):
    main = tmp_path / "ABC.jsonl"
    _write(main, '{"seq":0,"type":"session_start","session_id":"ABC","model":"m"}')
    child = tmp_path / "ABC" / "sandbox" / "eph-1" / "c.jsonl"
    _write(child, '{"seq":0,"type":"tool_call","session_id":"kid","parent_session_id":"ABC","tool":"run_shell","input":{}}')
    out = render_timeline(load_session_events(main))
    # 子事件那一行应带缩进（depth=1 → 两个空格前缀）
    kid_line = [l for l in out.splitlines() if "run_shell" in l][0]
    assert kid_line.startswith("  ")
