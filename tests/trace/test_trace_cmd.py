import json
from nanocode.entrypoints import trace_cmd


def _write_trace(tmp_path):
    d = tmp_path / ".nanocode" / "traces"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "abc12345.jsonl", "w", encoding="utf-8") as f:
        for e in [
            {"seq": 0, "type": "session_start", "session_id": "abc12345", "parent_session_id": None,
             "ts": "2026-01-01T00:00:00+00:00", "model": "m", "permission_mode": "default"},
            {"seq": 1, "type": "user_message", "session_id": "abc12345", "parent_session_id": None, "text": "hi"},
            {"seq": 2, "type": "session_end", "session_id": "abc12345", "parent_session_id": None,
             "input_tokens": 100, "output_tokens": 20, "turns": 1, "ts": "2026-01-01T00:00:01+00:00"},
        ]:
            f.write(json.dumps(e) + "\n")


def test_run_list(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_trace(tmp_path)
    assert trace_cmd.run([]) == 0
    assert "abc12345" in capsys.readouterr().out


def test_run_timeline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_trace(tmp_path)
    assert trace_cmd.run(["abc"]) == 0
    assert "user_message" in capsys.readouterr().out


def test_run_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_trace(tmp_path)
    assert trace_cmd.run(["abc", "--summary"]) == 0
    assert "tokens:" in capsys.readouterr().out


def test_run_bad_id_returns_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_trace(tmp_path)
    assert trace_cmd.run(["nope_xyz"]) == 1
