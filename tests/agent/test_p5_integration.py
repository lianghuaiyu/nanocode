"""P5 集成：resume 权威翻转到 events（snapshot 兜底）、fork_to、/tree 渲染。"""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.runtime import AgentRuntime
from nanocode.session import v2 as _v2
from nanocode.trace.sinks import JsonlSink
from nanocode.trace.tracer import Tracer
from nanocode.trace import report
from nanocode.events import reader


def _agent(sid, **kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", trace_enabled=False, session_id=sid, **kw)


def _seed_wire(sid, rows, agent_id="main"):
    """直接写一条 wire（绕过真实 LLM），供 restore/rebuild 测试。"""
    t = Tracer(sid, [JsonlSink(_v2.agent_wire_path(sid, agent_id))], agent_id=agent_id)
    t.begin_turn()
    for typ, kwargs in rows:
        t.emit(typ, **kwargs)
    t.close()


def test_restore_session_prefers_events_over_snapshot():
    sid = "p5r1"
    _seed_wire(sid, [
        ("llm_request", {"model": "m", "messages": [{"role": "user", "content": "from-events"}]}),
        ("assistant_message", {"text": "ans", "tool_uses": []}),
    ])
    a = _agent(sid)  # __init__ appends a session_start to the same wire (harmless)
    # snapshot data 也给，但 events 为权威 → 用重建
    a.restore_session({"anthropicMessages": [{"role": "user", "content": "from-snapshot"}]})
    msgs = a._anthropic_messages
    assert msgs[0] == {"role": "user", "content": "from-events"}     # events 赢
    assert msgs[-1] == {"role": "assistant", "content": "ans"}


def test_restore_session_falls_back_to_snapshot_when_no_events():
    sid = "p5r2"
    a = _agent(sid)  # wire 只有 session_start，无 llm_request → 重建为空
    a.restore_session({"anthropicMessages": [{"role": "user", "content": "snap-fallback"}]})
    assert a._anthropic_messages == [{"role": "user", "content": "snap-fallback"}]


def test_runtime_fork_to_creates_isolated_branch():
    sid = "p5fk"
    _seed_wire(sid, [
        ("user_message", {"text": "base"}),
        ("llm_request", {"model": "m", "messages": [{"role": "user", "content": "base"}]}),  # evt_main_1 fork pt
        ("assistant_message", {"text": "bp", "tool_uses": []}),
    ])
    a = _agent(sid)
    th = AgentRuntime().adopt(a)
    ctx = th.fork_to("evt_main_1", "experiment")
    # fork 上下文 = fork 点重建（base）
    assert ctx == [{"role": "user", "content": "base"}]
    # tracer 切到新分支，后续 emit 带 branch_id=experiment + 首事件 parent_event_id=fork 点
    a.tracer.emit("user_message", text="on-branch")
    evs = reader.read_agent_wire(_v2.agent_wire_path(sid, "main"), "main")
    branch_evs = [e for e in evs if e.branch_id == "experiment"]
    assert branch_evs and branch_evs[0].parent_event_id == "evt_main_1"


def test_tree_render_shows_branches_and_fork_points():
    sid = "p5tree"
    t = Tracer(sid, [JsonlSink(_v2.agent_wire_path(sid, "main"))], agent_id="main")
    t.begin_turn()
    t.emit("user_message", text="root q")
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "root q"}])  # evt_main_1
    t.emit("turn_end", input_tokens=1, output_tokens=1)
    t.begin_branch("experiment", from_event_id="evt_main_1")
    t.emit("user_message", text="branch q")
    t.close()
    events = reader.merge_session_events(sid)
    out = report.render_wire_tree(events)
    assert "branch main" in out
    assert "branch experiment" in out
    assert "forked from evt_main_1" in out
    assert "root q" in out and "branch q" in out
