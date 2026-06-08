"""Tracer 的 entry-tree enrich：id/parent_id/turn_id/branch_id/agent_id + resume 续号。

属事件 spine 的写侧验证（schema 见 events.models；读侧见 events.reader）。
"""

from nanocode.trace.tracer import Tracer, NullTracer
from nanocode.events.models import is_legacy


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


def test_emit_stamps_envelope_and_chains_parent():
    sink = MemSink()
    t = Tracer("sess", [sink], agent_id="main")
    t.emit("user_message", text="hi")
    t.emit("tool_call", tool="grep", tool_use_id="tu_1")
    e0, e1 = sink.events
    # id 确定性、会话内唯一
    assert e0["id"] == "evt_main_0" and e1["id"] == "evt_main_1"
    assert e0["agent_id"] == "main" and e0["branch_id"] == "main"
    # parent 链：首条 None，次条指向首条
    assert e0["parent_id"] is None
    assert e1["parent_id"] == "evt_main_0"
    # 非 legacy（带 envelope id）
    assert not is_legacy(e0)
    # payload 仍扁平（flat-additive，不破坏 report.py）
    assert e0["text"] == "hi" and e1["tool"] == "grep" and e1["tool_use_id"] == "tu_1"


def test_turn_id_none_until_begin_turn_and_is_resume_safe():
    sink = MemSink()
    t = Tracer("sess", [sink], agent_id="main")
    t.emit("session_start")            # turn 前
    tid = t.begin_turn()
    t.emit("user_message", text="x")   # turn 内（seq 1）
    assert sink.events[0]["turn_id"] is None
    # turn_id 由 resume-safe 的 seq 派生（本 turn 首个事件的 seq = 1）
    assert tid == "turn_main_1"
    assert sink.events[1]["turn_id"] == "turn_main_1"
    # 下一个 turn 用新的 seq，跨 resume 不碰撞
    assert t.begin_turn() == "turn_main_2"
    t.emit("user_message", text="y")
    assert sink.events[2]["turn_id"] == "turn_main_2"


def test_begin_turn_accepts_explicit_id():
    sink = MemSink()
    t = Tracer("sess", [sink])
    assert t.begin_turn("turn_custom") == "turn_custom"
    t.emit("user_message")
    assert sink.events[0]["turn_id"] == "turn_custom"


def test_turn_id_does_not_collide_across_resume():
    """两个独立 Tracer（模拟 resume）：turn_id 因 seq 续号而不碰撞。"""
    s1 = MemSink()
    t1 = Tracer("sess", [s1], agent_id="main", start_seq=0)
    tid1 = t1.begin_turn()
    t1.emit("user_message")  # seq 0
    # resume：新 tracer 从 tail 续号 start_seq=1
    s2 = MemSink()
    t2 = Tracer("sess", [s2], agent_id="main", start_seq=1)
    tid2 = t2.begin_turn()
    t2.emit("user_message")  # seq 1
    assert tid1 == "turn_main_0" and tid2 == "turn_main_1"
    assert tid1 != tid2  # 旧实现会两次都给 turn_1


def test_resume_continues_seq_and_links_to_prior_tail():
    """start_seq>0（resume 续号）：id 从该 seq 起，首条 parent = 上一轮 tail 的反推 id。"""
    sink = MemSink()
    t = Tracer("sess", [sink], agent_id="main", start_seq=3)
    t.emit("user_message", text="resumed")
    e = sink.events[0]
    assert e["seq"] == 3
    assert e["id"] == "evt_main_3"
    # 跨 resume 链接：parent 指向上一轮的 tail（evt_main_2），即便那条 legacy 也能反推
    assert e["parent_id"] == "evt_main_2"


def test_custom_agent_id_used_in_ids():
    sink = MemSink()
    t = Tracer("sess", [sink], agent_id="agent-007")
    t.emit("user_message")
    assert sink.events[0]["id"] == "evt_agent-007_0"
    assert sink.events[0]["agent_id"] == "agent-007"


def test_instrumentation_never_crashes_agent_and_seq_advances():
    t = Tracer("sess", [BoomSink()])
    # sink 抛错不得冒泡
    t.emit("user_message")
    t.emit("tool_call")
    assert t._seq == 2  # envelope/seq 仍推进


def test_child_gets_distinct_agent_id_and_parent_session():
    sink = MemSink()
    parent = Tracer("parent", [sink], agent_id="main")
    child = parent.child("kid")
    child.emit("user_message")
    e = sink.events[0]
    assert e["session_id"] == "kid"
    assert e["parent_session_id"] == "parent"
    assert e["agent_id"] == "kid" and e["id"] == "evt_kid_0"


def test_null_tracer_begin_turn_and_emit_are_noops():
    n = NullTracer()
    assert n.begin_turn() == ""
    n.emit("anything", x=1)  # 不抛
    assert n.child("k") is n
