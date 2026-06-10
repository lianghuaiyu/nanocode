"""durable wire schema 契约 guard：锁住 RUNTIME-P1（durable 事件流）↔ Trajectory（投影）的桥。

trajectory.project / metrics / eval 与 resume 重建都依赖具体 durable type 的稳定 payload 字段
（runtime_events.DURABLE_EVENT_FIELDS）。改任一 durable type 或字段名而不更新契约 = 静默破坏
trajectory。本测试让这种漂移变成**失败的测试**：

1. 契约的 type 集 == DURABLE_TYPES（增删 durable type 必须同步契约）。
2. 已迁移到单流 dispatch 的 emit（tool_call/tool_result/assistant_message）落到 wire 的 payload
   字段 == 契约（防 dispatch 改 payload）。
3. 用**只含契约字段**的合成 wire 跑 trajectory.build_steps + compute_metrics，证明投影只需契约字段。
4. SUMMARY 整形（trace.redaction）对 llm_request/tool_result 的字段替换与契约一致。
"""

from nanocode.agent import runtime_events as re
from nanocode.agent.sink import NullSink
from nanocode.events.models import SessionEvent
from nanocode.trace.redaction import apply_summary_shaping
from nanocode.trajectory.metrics import compute_metrics
from nanocode.trajectory.project import build_steps


class _RecTracer:
    def __init__(self): self.emits = []
    def emit(self, type, **fields): self.emits.append((type, fields))


# ─── 1. 契约 ↔ DURABLE_TYPES ─────────────────────────────────────

def test_contract_covers_exactly_durable_types():
    assert set(re.DURABLE_EVENT_FIELDS) == set(re.DURABLE_TYPES)


# ─── 2. 已迁移 emit 的 payload == 契约 ────────────────────────────

def test_migrated_dispatch_emits_match_contract():
    """已迁移到单流 dispatch 的 emit（tool_call/tool_result/assistant_message）落 wire 的 payload
    字段 == 契约。注：本测试钉的是 **dispatch 层**保字段；backend 调用点传入的 kwargs 与契约一致
    由 Codex review 逐站点核验，端到端 drive-backend 守护属后续强化项（MEDIUM）。"""
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", trace_enabled=False, session_id="contractsid")
    a.tracer, a._sink = _RecTracer(), NullSink()
    a._dispatch_event("tool_call", tool="run_shell", input={"command": "ls"}, tool_use_id="x")
    a._dispatch_event("tool_result", tool="run_shell", tool_use_id="x", chars=2, result="ok")
    a._dispatch_event("assistant_message", text="hi", thinking="", tool_uses=[])
    by_type = {t: set(f) for t, f in a.tracer.emits}
    for t in ("tool_call", "tool_result", "assistant_message"):
        assert by_type[t] == set(re.DURABLE_EVENT_FIELDS[t]), f"{t} emit drifted from contract"


# ─── 3. trajectory 只用契约字段即可投影 ───────────────────────────

def _ev(seq, etype, **fields):
    return SessionEvent.from_wire(
        {"v": 1, "session_id": "s", "agent_id": "main", "branch_id": "main",
         "type": etype, "ts": f"2026-06-10T00:00:{seq:02d}Z", "seq": seq,
         "id": f"evt_main_{seq}", "turn_id": "turn_main_1", **fields},
        agent_id="main")


def test_trajectory_projects_from_contracted_fields_only():
    events = [
        _ev(0, "session_start", model="m", cwd="/", permission_mode="default",
            is_sub_agent=False, workspace_trusted=True),
        _ev(1, "user_message", text="do it"),
        _ev(2, "llm_request", model="m", message_count=2,
            messages=[{"role": "user", "content": "do it"}]),
        _ev(3, "assistant_message", text="calling", thinking="",
            tool_uses=[{"id": "x", "name": "run_shell", "input": {"command": "ls"}}]),
        _ev(4, "llm_response", input_tokens=10, output_tokens=3),
        _ev(5, "tool_call", tool="run_shell", input={"command": "ls"}, tool_use_id="x"),
        _ev(6, "tool_result", tool="run_shell", tool_use_id="x", chars=2, result="ok"),
        _ev(7, "turn_end", input_tokens=10, output_tokens=3, turns=1),
        _ev(8, "session_end", input_tokens=10, output_tokens=3, turns=1),
    ]
    steps = build_steps(events)
    types = {s.step_type for s in steps}
    assert {"tool_action", "llm_decision", "final"} <= types
    ta = next(s for s in steps if s.step_type == "tool_action")
    assert ta.action.get("tool") == "run_shell"      # 读 tool_call.tool
    assert ta.result_summary == "ok"                  # 读 tool_result.result
    dec = next(s for s in steps if s.step_type == "llm_decision")
    assert dec.input_tokens == 10 and dec.output_tokens == 3   # 读 llm_response tokens
    assert any(s.step_type == "final" and s.done for s in steps)  # session_end → done
    m = compute_metrics(events, steps)
    assert isinstance(m, dict) and m                  # metrics 在契约事件上不崩、非空


# ─── 4. SUMMARY 整形 ↔ 契约 ──────────────────────────────────────

def test_summary_shaping_matches_contract():
    e = {"type": "llm_request", "model": "m", "message_count": 2,
         "messages": [{"role": "user", "content": "hi"}]}
    apply_summary_shaping(e)
    # 精确锁后整形键集：messages 丢弃 → 补 messages_chars/messages_hash；type/model/message_count 原样保留。
    assert set(e) == {"type", "model", "message_count", "messages_chars", "messages_hash"}
    assert e["message_count"] == 2                     # 轻量契约字段保留

    r = {"type": "tool_result", "tool": "run_shell", "tool_use_id": "x",
         "chars": 5, "result": "hello"}
    apply_summary_shaping(r)
    assert set(r) == {"type", "tool", "tool_use_id", "chars", "result_summary", "result_hash"}
    assert r["chars"] == 5
