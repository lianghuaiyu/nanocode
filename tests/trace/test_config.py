from pathlib import Path
from nanocode.trace import config, make_tracer, NullTracer, Tracer


def test_is_enabled_flag():
    assert config.is_enabled(True) is True


def test_is_enabled_env(monkeypatch):
    monkeypatch.delenv("NANOCODE_TRACE", raising=False)
    assert config.is_enabled(False) is False
    monkeypatch.setenv("NANOCODE_TRACE", "1")
    assert config.is_enabled(False) is True
    monkeypatch.setenv("NANOCODE_TRACE", "yes")
    assert config.is_enabled(False) is True
    monkeypatch.setenv("NANOCODE_TRACE", "0")
    assert config.is_enabled(False) is False


def test_trace_dir_is_project_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = config.trace_dir()
    assert d == tmp_path / ".nanocode" / "traces"
    assert d.is_dir()


def test_disabled_tracer_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = make_tracer("sess", enabled=False)
    assert isinstance(t, NullTracer)
    t.emit("user_message", text="hi")
    t.close()
    assert not (tmp_path / ".nanocode").exists()   # 关闭态零侵入：不建任何目录/文件


def test_enabled_tracer_writes_project_local_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = make_tracer("sess", enabled=True)
    assert isinstance(t, Tracer)
    t.emit("user_message", text="hi")
    t.close()
    f = tmp_path / ".nanocode" / "traces" / "sess.jsonl"
    assert f.exists() and "user_message" in f.read_text()
