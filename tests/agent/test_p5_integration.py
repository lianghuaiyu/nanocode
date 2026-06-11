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


def test_restore_main_ignores_wire_events_uses_snapshot_fallback():
    # docs/14 §4.2：main restore 不再从 wire 事件重建（canonical 树才是权威）。无树 → 回退快照。
    sid = "p5r1"
    _seed_wire(sid, [
        ("llm_request", {"model": "m", "messages": [{"role": "user", "content": "from-events"}]}),
        ("assistant_message", {"text": "ans", "tool_uses": []}),
    ])
    a = _agent(sid)
    a.restore_session({"anthropicMessages": [{"role": "user", "content": "from-snapshot"}]})
    assert a._anthropic_messages == [{"role": "user", "content": "from-snapshot"}]   # 快照兜底，非 wire


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


def test_fork_to_invalid_event_raises_and_leaves_session_unchanged():
    """Codex P2: fork_to 无效 from_event_id 不得静默清空 live 历史。"""
    sid = "p5fkbad"
    _seed_wire(sid, [
        ("user_message", {"text": "base"}),
        ("llm_request", {"model": "m", "messages": [{"role": "user", "content": "base"}]}),
    ])
    a = _agent(sid)
    a._append_message({"role": "user", "content": "live"})
    th = AgentRuntime().adopt(a)
    before = list(a._anthropic_messages)
    before_branch = a.tracer.branch_id
    try:
        th.fork_to("evt_main_999", "bad")   # 不存在的 event id
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert a._anthropic_messages == before           # live 历史未被清
    assert a.tracer.branch_id == before_branch        # 未切到无效分支


def test_restore_falls_back_to_snapshot_when_rebuild_unfaithful():
    """blocking 数据丢失回归：turn 在 tool 执行后被打断（无第二个 llm_request）时，
    events 重建会丢 tool 轮——restore 必须回退到完整 snapshot，不丢数据。"""
    sid = "p5unfaithful"
    _seed_wire(sid, [
        ("llm_request", {"model": "m", "messages": [{"role": "user", "content": "q"}]}),
        ("assistant_message", {"text": "working", "tool_uses": [{"id": "tu", "name": "read_file", "input": {}}]}),
        ("tool_result", {"tool": "read_file", "tool_use_id": "tu", "result": "FILE CONTENTS"}),
    ])
    a = _agent(sid)
    # snapshot 保存了完整 3 条（含 tool 输出）——_auto_save 的实际行为
    full = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tu", "name": "read_file"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu", "content": "FILE CONTENTS"}]},
    ]
    a.restore_session({"anthropicMessages": full})
    # 重建不忠实 → 回退 snapshot → tool 输出仍在
    assert a._anthropic_messages == full
    flat = str(a._anthropic_messages)
    assert "FILE CONTENTS" in flat


def test_restore_main_does_not_continue_wire_fork_branch():
    """docs/14 §4.2 / risk#5：main restore 不再从 wire 续 fork 分支（树才是权威，wire branch_id 退化为 main）。
    live forking 仍由 AgentSession.fork_to 经 wire 表达（见 test_runtime_fork_to_creates_isolated_branch）。"""
    sid = "p5rbranch"
    t = Tracer(sid, [JsonlSink(_v2.agent_wire_path(sid, "main"))], agent_id="main")
    t.begin_turn()
    t.emit("user_message", text="base")
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "base"}])  # evt_main_1
    t.begin_branch("experiment", from_event_id="evt_main_1")
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "on-exp"}])
    t.emit("assistant_message", text="exp-ans", tool_uses=[])
    t.close()
    a = _agent(sid)
    a.restore_session({})                       # 无树、无 legacy → 空，tracer 留默认 main
    assert a.tracer.branch_id == "main"         # 不再从 wire 续 experiment 分支
    assert a._anthropic_messages == []          # wire 不是 main resume 源
