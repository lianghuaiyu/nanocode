from nanocode.trace.tracer import Tracer, NullTracer, make_tracer, SCHEMA_VERSION


class MemSink:
    def __init__(self):
        self.events = []
    def write(self, event):
        self.events.append(event)
    def close(self):
        pass


class BoomSink:
    def write(self, event):
        raise RuntimeError("boom")
    def close(self):
        raise RuntimeError("boom")


def test_emit_builds_event_and_increments_seq():
    sink = MemSink()
    t = Tracer("sess1", [sink])
    t.emit("user_message", text="hi")
    t.emit("tool_call", tool="read_file")
    assert [e["seq"] for e in sink.events] == [0, 1]
    e0 = sink.events[0]
    assert e0["v"] == SCHEMA_VERSION
    assert e0["session_id"] == "sess1"
    assert e0["parent_session_id"] is None
    assert e0["type"] == "user_message" and e0["text"] == "hi"
    assert "ts" in e0


def test_child_links_parent_and_shares_sinks():
    sink = MemSink()
    parent = Tracer("parent", [sink])
    child = parent.child("kid")
    child.emit("session_start")
    assert child.parent_session_id == "parent"
    assert child.sinks is parent.sinks
    assert sink.events[0]["parent_session_id"] == "parent"


def test_emit_swallows_sink_exceptions():
    t = Tracer("s", [BoomSink()])
    t.emit("x")        # 不得抛
    t.close()          # 不得抛


def test_null_tracer_is_noop():
    t = NullTracer()
    t.emit("x", a=1)
    assert t.child("k") is t
    t.close()          # 全部 no-op、不抛


def test_make_tracer_disabled_returns_null():
    t = make_tracer("s", enabled=False)
    assert isinstance(t, NullTracer)


def test_make_tracer_enabled_with_explicit_sinks():
    sink = MemSink()
    t = make_tracer("s", enabled=True, sinks=[sink])
    assert isinstance(t, Tracer)
    t.emit("x")
    assert sink.events[0]["type"] == "x"
