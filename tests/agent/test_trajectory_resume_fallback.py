"""trajectory SUMMARY 降级边界：wire 无完整 messages 时，resume 回退 snapshot（不丢上下文）。

证明硬边界：FULL 级 wire 可被 event-tree rebuild；SUMMARY 级 wire 丢掉重型 payload
（llm_request.messages 被 pop + hash），event-tree rebuild 因此退化 —— SessionContextBuilder
按既有 faithful/empty 判定回退到 snapshot（messages.json），resume 上下文 byte-exact 无丢失。
"""
from nanocode.session import v2 as _v2
from nanocode.trace.sinks import JsonlSink
from nanocode.trace.tracer import Tracer
from nanocode.agent.context_builder import SessionContextBuilder


def test_summary_wire_resume_falls_back_to_snapshot_no_context_loss():
    sid = "traj_resume_sid"
    # 1) 真实 v2 snapshot（resume 兜底权威）
    snapshot = [
        {"role": "user", "content": "implement feature X"},
        {"role": "assistant", "content": "done"},
    ]
    _v2.write_main_messages(sid, snapshot)

    # 2) SUMMARY 级 trajectory tracer 写真实 wire（JsonlSink @ agent_wire_path）
    wire_path = _v2.agent_wire_path(sid, "main")
    t = Tracer(
        sid,
        [JsonlSink(wire_path)],
        agent_id="main",
        trajectory_enabled=True,
        trajectory_level="summary",
    )
    t.begin_turn()
    # llm_request 携带完整 messages 入 emit —— SUMMARY 整形会把 messages pop 掉、只留 hash
    t.emit(
        "llm_request",
        model="m",
        messages=[{"role": "user", "content": "implement feature X"}],
        message_count=1,
    )
    t.emit("assistant_message", text="done", tool_uses=[])
    t.close()

    b = SessionContextBuilder(sid)
    # resume 优先事件 —— 但 SUMMARY wire 无 messages，rebuild 退化 → 回退 snapshot（byte-exact）
    assert b.resume_messages(prefer_events=True) == snapshot
    # 事件树重建本身为空（没有可用的 llm_request.messages 快照）
    assert b.rebuild_messages() == []

    # 3) 盘上 llm_request 事件：有 trajectory + messages_hash，但**无** messages payload
    from nanocode.events import reader as _reader

    events = _reader.read_agent_wire(wire_path, "main")
    reqs = [e for e in events if e.type == "llm_request"]
    assert len(reqs) == 1
    req = reqs[0]
    assert req.data.get("trajectory") is True
    assert isinstance(req.data.get("messages_hash"), str)
    assert req.data["messages_hash"].startswith("sha256:")
    assert "messages" not in req.data  # 重型 payload 被丢弃
    assert req.data.get("message_count") == 1  # 既有摘要字段保留


def test_summary_wire_closed_tool_round_also_falls_back_to_snapshot():
    """多轮 / 已闭合 tool 轮的 SUMMARY 降级也回退 snapshot（锁定边界，审阅建议补充）。

    单轮开放 case 已由上面覆盖；这里造一个**已闭合**的 tool 轮（assistant tool_use →
    tool_result → 第二个 llm_request 把它纳入快照），但全部 SUMMARY 整形（llm_request.messages
    被 pop）。即便闭合，wire 上也无任何 messages 数组可作 rebuild oracle，故仍退化 snapshot。
    """
    sid = "traj_resume_closed_sid"
    snapshot = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "finished"},
    ]
    _v2.write_main_messages(sid, snapshot)

    wire_path = _v2.agent_wire_path(sid, "main")
    t = Tracer(sid, [JsonlSink(wire_path)], agent_id="main",
               trajectory_enabled=True, trajectory_level="summary")
    t.begin_turn()
    t.emit("llm_request", model="m", message_count=1,
           messages=[{"role": "user", "content": "do it"}])
    t.emit("assistant_message", text="",
           tool_uses=[{"id": "tu", "name": "read_file", "input": {}}])
    t.emit("tool_result", tool="read_file", tool_use_id="tu", chars=1, result="x")
    # 第二个 llm_request：闭合上面的 tool 轮（含 assistant tool_use + tool_result）。
    t.emit("llm_request", model="m", message_count=3,
           messages=[{"role": "user", "content": "do it"},
                     {"role": "assistant", "content": [{"type": "tool_use", "id": "tu"}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu", "content": "x"}]}])
    t.emit("assistant_message", text="finished", tool_uses=[])
    t.close()

    b = SessionContextBuilder(sid)
    assert b.rebuild_messages() == []                       # 无 messages 数组可重建
    assert b.resume_messages(prefer_events=True) == snapshot  # 回退 snapshot，无上下文丢失
    # 两个 llm_request 都被整形（无 messages payload，带 hash）。
    from nanocode.events import reader as _reader
    reqs = [e for e in _reader.read_agent_wire(wire_path, "main") if e.type == "llm_request"]
    assert len(reqs) == 2
    assert all("messages" not in r.data and "messages_hash" in r.data for r in reqs)
