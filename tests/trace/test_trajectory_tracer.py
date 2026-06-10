"""trajectory 在 Tracer 上的信封注入 + summary 整形 + FULL/disabled byte-identical。"""
import json

from nanocode.trace.tracer import Tracer, NullTracer


class MemSink:
    def __init__(self):
        self.events = []
    def write(self, event):
        self.events.append(event)
    def close(self):
        pass


def test_every_event_carries_trajectory_envelope():
    sink = MemSink()
    t = Tracer("sid", [sink], trajectory_enabled=True, trajectory_level="full")
    t.emit("user_message", text="hi")
    t.emit("session_start")
    for e in sink.events:
        assert e["trajectory"] is True
        assert e["trajectory_id"] == "traj_sid"
        assert e["trajectory_level"] == "full"


def test_trajectory_id_derivation_and_override():
    s1 = MemSink()
    t1 = Tracer("abc", [s1], trajectory_enabled=True)
    t1.emit("x")
    assert s1.events[0]["trajectory_id"] == "traj_abc"
    s2 = MemSink()
    t2 = Tracer("abc", [s2], trajectory_enabled=True, trajectory_id="traj_custom")
    t2.emit("x")
    assert s2.events[0]["trajectory_id"] == "traj_custom"


def test_summary_drops_messages_on_llm_request():
    sink = MemSink()
    t = Tracer("sid", [sink], trajectory_enabled=True, trajectory_level="summary")
    messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    t.emit("llm_request", model="m", messages=messages, message_count=2)
    e = sink.events[0]
    assert "messages" not in e
    assert e["message_count"] == 2
    assert e["messages_chars"] == len(json.dumps(messages, ensure_ascii=False, default=str))
    assert e["messages_hash"].startswith("sha256:")
    assert e["trajectory_level"] == "summary"


def test_summary_drops_result_on_tool_result():
    sink = MemSink()
    t = Tracer("sid", [sink], trajectory_enabled=True, trajectory_level="summary")
    big = "X" * 5000
    t.emit("tool_result", tool="read_file", tool_use_id="tu", result=big, chars=5000)
    e = sink.events[0]
    assert "result" not in e
    assert e["chars"] == 5000
    assert e["result_summary"].startswith("X")
    assert len(e["result_summary"]) < 5000  # 被截断
    assert e["result_hash"].startswith("sha256:")


def test_full_level_keeps_messages_and_result_byte_identical():
    sink = MemSink()
    t = Tracer("sid", [sink], trajectory_enabled=True, trajectory_level="full")
    messages = [{"role": "user", "content": "q"}]
    big = "Y" * 4000
    t.emit("llm_request", model="m", messages=messages, message_count=1)
    t.emit("tool_result", tool="read_file", tool_use_id="tu", result=big, chars=4000)
    req, res = sink.events
    assert req["messages"] == messages
    assert "messages_chars" not in req and "messages_hash" not in req
    assert res["result"] == big
    assert "result_summary" not in res and "result_hash" not in res


def test_disabled_has_no_trajectory_keys_and_identical_payload():
    sink = MemSink()
    t = Tracer("sid", [sink])  # trajectory disabled (default)
    messages = [{"role": "user", "content": "q"}]
    big = "Z" * 4000
    t.emit("llm_request", model="m", messages=messages, message_count=1)
    t.emit("tool_result", tool="read_file", result=big, chars=4000)
    req, res = sink.events
    assert "trajectory" not in req and "trajectory_id" not in req and "trajectory_level" not in req
    assert req["messages"] == messages
    assert "messages_hash" not in req
    assert res["result"] == big
    assert "result_hash" not in res


def test_disabled_tracer_default_attr():
    sink = MemSink()
    t = Tracer("sid", [sink])
    assert t.trajectory_enabled is False
    assert t.trajectory_level == "summary"
    assert t.trajectory_id is None


def test_child_propagates_trajectory():
    sink = MemSink()
    parent = Tracer("p", [sink], trajectory_enabled=True, trajectory_level="full",
                    trajectory_id="traj_p")
    child = parent.child("kid", agent_id="agent-001")
    assert child.trajectory_enabled is True
    assert child.trajectory_level == "full"
    assert child.trajectory_id == "traj_p"
    child.emit("x")
    assert sink.events[0]["trajectory_id"] == "traj_p"


def test_null_tracer_trajectory_attrs():
    t = NullTracer()
    assert t.trajectory_enabled is False
    assert t.trajectory_level == "summary"
    assert t.trajectory_id is None
    t.emit("x")  # 仍 no-op、不抛
