"""wire-tree 渲染（report.render_wire_tree）——分支 / fork 点展示。

docs/14 SessionLease：原 P5 的「resume 从 wire/snapshot 重建 + AgentSession.fork_to」已退役——
resume 现由 runtime SessionLease + cli._load_from_manager 从 canonical 树重建（见
tests/session/test_p3_resume.py），`restore_session` 已删。本文件仅保留 wire-tree 渲染用例
（trace/wire debug lane；整条 trace/wire 线在 Milestone B 退役，届时本文件随之移除）。
"""

from __future__ import annotations

from nanocode.session import v2 as _v2
from nanocode.trace.sinks import JsonlSink
from nanocode.trace.tracer import Tracer
from nanocode.trace import report
from nanocode.events import reader


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
