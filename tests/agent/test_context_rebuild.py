"""P5：SessionContextBuilder 事件树重建（llm_request 快照 oracle + 尾条 assistant + 分支）。"""

from __future__ import annotations

from nanocode.session import v2 as _v2
from nanocode.trace.sinks import JsonlSink
from nanocode.trace.tracer import Tracer
from nanocode.agent.context_builder import SessionContextBuilder


def _wire(session_id: str, agent_id: str = "main") -> Tracer:
    return Tracer(session_id, [JsonlSink(_v2.agent_wire_path(session_id, agent_id))], agent_id=agent_id)


def test_rebuild_uses_last_llm_request_plus_trailing_assistant():
    sid = "cb1"
    t = _wire(sid); t.begin_turn()
    t.emit("user_message", text="q")
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "q"}])
    t.emit("assistant_message", text="", tool_uses=[{"id": "tu", "name": "read_file", "input": {}}])
    t.emit("tool_result", tool="read_file", tool_use_id="tu", result="r")
    seen = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu", "content": "r"}]}]
    t.emit("llm_request", model="m", messages=seen)
    t.emit("assistant_message", text="done", tool_uses=[])
    t.close()

    rebuilt = SessionContextBuilder(sid).rebuild_messages()
    assert rebuilt[:3] == seen                 # byte-exact 模型所见
    assert rebuilt[3] == {"role": "assistant", "content": "done"}


def test_rebuild_empty_when_no_llm_request():
    sid = "cb2"
    t = _wire(sid); t.begin_turn(); t.emit("user_message", text="q"); t.close()
    assert SessionContextBuilder(sid).rebuild_messages() == []


def test_unfaithful_rebuild_when_tool_round_after_last_llm_request():
    """blocking 数据丢失回归（Codex/workflow）：turn 在 tool 执行后、第二个 llm_request 前
    被打断（abort/budget/turn-limit）——wire 有 llm_request→assistant(tool_uses)→tool_result，
    无第二个 llm_request。重建会丢掉该 assistant tool_use + tool_result，故必须判为不忠实，
    让调用方回退 snapshot。"""
    sid = "cb3"
    t = _wire(sid); t.begin_turn()
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "q"}])
    t.emit("assistant_message", text="working", tool_uses=[{"id": "tu", "name": "read_file", "input": {}}])
    t.emit("tool_result", tool="read_file", tool_use_id="tu", result="FILE CONTENTS")
    t.close()
    b = SessionContextBuilder(sid)
    msgs, faithful = b._rebuild()
    assert faithful is False                       # 不忠实——尾部有未闭合 tool 轮
    # resume 据此回退 snapshot（此处无 snapshot → 空），绝不返回丢了 tool 输出的残缺重建
    assert b.resume_messages(prefer_events=True) == b.snapshot_messages()


def test_rebuild_branch_from_leaf_only_walks_ancestors():
    """leaf_id 指定时只取该 leaf 沿 parent_id 可达的事件（fork 分支隔离）。"""
    sid = "cb4"
    t = _wire(sid); t.begin_turn()
    t.emit("user_message", text="root")                       # evt_main_0
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "root"}])  # evt_main_1
    t.emit("assistant_message", text="branchpoint", tool_uses=[])  # evt_main_2
    # 之后又来一轮（更晚的 leaf），但我们从 evt_main_1 这个更早的 leaf 重建
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "later"}])  # evt_main_3
    t.close()
    b = SessionContextBuilder(sid)
    # 从 evt_main_1 重建：只含到 evt_main_1 的链，最后 llm_request = evt_main_1 的 [root]
    rebuilt = b.rebuild_messages(leaf_id="evt_main_1")
    assert rebuilt == [{"role": "user", "content": "root"}]
    # 不带 leaf（默认全量）→ 用最后的 llm_request（later）
    assert b.rebuild_messages() == [{"role": "user", "content": "later"}]


def test_resume_messages_defaults_to_snapshot_prefer_events_opt_in():
    sid = "cb5"
    _v2.write_main_messages(sid, [{"role": "user", "content": "snap"}])
    t = _wire(sid); t.begin_turn()
    t.emit("llm_request", model="m", messages=[{"role": "user", "content": "evt"}])
    t.emit("assistant_message", text="a", tool_uses=[])
    t.close()
    b = SessionContextBuilder(sid)
    # 默认 snapshot（P3 行为保持）
    assert b.resume_messages() == [{"role": "user", "content": "snap"}]
    # 显式 prefer_events → 事件树重建
    assert b.resume_messages(prefer_events=True)[0] == {"role": "user", "content": "evt"}
