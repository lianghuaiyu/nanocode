from pathlib import Path
from nanocode.trace.config import trace_dir


def test_trace_dir_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom_traces"
    monkeypatch.setenv("NANOCODE_TRACE_DIR", str(target))
    assert trace_dir() == target
    assert target.is_dir()   # 被创建


def test_trace_dir_default_when_unset(monkeypatch):
    monkeypatch.delenv("NANOCODE_TRACE_DIR", raising=False)
    d = trace_dir()
    assert d.name == "traces"
    assert d.parent.name == ".nanocode"


from nanocode.trace.tracer import make_tracer


class _CaptureSink:
    def __init__(self):
        self.events = []
    def write(self, event):
        self.events.append(event)
    def close(self):
        pass


def test_make_tracer_parent_from_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_TRACE_PARENT", "ABC")
    sink = _CaptureSink()
    t = make_tracer("child-session", enabled=True, sinks=[sink])
    t.emit("tool_call", tool="x")
    assert sink.events[0]["parent_session_id"] == "ABC"
    assert sink.events[0]["session_id"] == "child-session"


def test_make_tracer_no_parent_when_unset(monkeypatch):
    monkeypatch.delenv("NANOCODE_TRACE_PARENT", raising=False)
    sink = _CaptureSink()
    t = make_tracer("solo", enabled=True, sinks=[sink])
    t.emit("tool_call", tool="x")
    assert sink.events[0]["parent_session_id"] is None
