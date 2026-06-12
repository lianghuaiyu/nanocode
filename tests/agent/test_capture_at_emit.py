"""docs/16 #0：capture-at-emit parity。

事件反转（STEP D-1）的唯一致命陷阱：core 持有 provider-shaped 消息，而
`AgentSession.record_event._append_neutral` 假设 `event.message` 已是中立 Message。
`events_from_provider_message` 必须与 `engine._tree_record` 的内联 capture 路径**树级等价**——
两条路径写进 canonical 树的 MESSAGE entry（modulo timestamp）必须相同。
"""

import pytest

from nanocode.agent.engine import Agent
from nanocode.agent.session import AgentSession
from nanocode.agent.events import (
    AssistantMessageCompleted,
    ToolResultCompleted,
    UserMessageAccepted,
    events_from_provider_message,
)
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _agent(sid, *, use_openai=False):
    kw = dict(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    if use_openai:
        kw.update(api_base="http://localhost:1/v1", model="gpt-test")
    return Agent(**kw)


def _strip_ts(msg: dict) -> dict:
    m = dict(msg)
    m.pop("timestamp", None)
    return m


def _tree_messages(sid: str) -> list[dict]:
    return [_strip_ts(e.data["message"]) for e in SessionManager.open(sid).entries()
            if e.type == T.MESSAGE]


# ── 语料：anthropic / openai 的全消息形态 ────────────────────────────────────

ANTHROPIC_CORPUS = [
    # (provider_msg, kwargs for _tree_record / factory)
    ({"role": "user", "content": "hello"}, {}),
    ({"role": "user", "content": [{"type": "text", "text": "look"},
                                  {"type": "image", "source": {"data": "B64", "media_type": "image/png"}}]}, {}),
    ({"role": "assistant", "content": [{"type": "text", "text": "plain answer"}]},
     {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 3}, "latency_ms": 1234}),
    ({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm", "signature": "SIG"},
        {"type": "redacted_thinking", "data": "RDATA"},
        {"type": "text", "text": "doing it"},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}},
    ]}, {"stop_reason": "tool_use", "usage": {"input_tokens": 7, "output_tokens": 2}, "latency_ms": 55}),
    ({"role": "assistant", "content": [{"type": "text", "text": "cut off"}]},
     {"stop_reason": "max_tokens"}),
    # tool_result-user 批量：两条 result（一条 error、一条带 per-block 延迟）
    ({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "toolName": "read_file",
         "content": "file body", "toolLatencyMs": 42},
        {"type": "tool_result", "tool_use_id": "tu_2", "toolName": "run_shell",
         "content": "boom", "is_error": True},
    ]}, {"latency_ms": 99}),
]

OPENAI_CORPUS = [
    ({"role": "user", "content": "hi"}, {}),
    ({"role": "assistant", "content": "text only"},
     {"stop_reason": "stop", "usage": {"input_tokens": 5, "output_tokens": 1}, "latency_ms": 88}),
    ({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "grep", "arguments": '{"pattern": "x"}'}},
        {"id": "c2", "function": {"name": "ls", "arguments": "NOT-JSON{"}},     # bad args → argumentsRaw 审计
    ]}, {"stop_reason": "tool_calls"}),
    ({"role": "tool", "tool_call_id": "c1", "content": "grep out"}, {"latency_ms": 7}),
    ({"role": "system", "content": "sys"}, {}),                                 # → 0 条（system 不入树）
]


# ── 树级 parity：factory→record_event 与 _tree_record 写出的树相同 ────────────

@pytest.mark.parametrize("use_openai,corpus", [(False, ANTHROPIC_CORPUS), (True, OPENAI_CORPUS)],
                         ids=["anthropic", "openai"])
def test_tree_parity_with_inline_tree_record(use_openai, corpus):
    provider = "openai" if use_openai else "anthropic"
    a1 = _agent(f"cap0a_{provider}", use_openai=use_openai)
    a1._session_mgr = SessionManager.create(f"cap0a_{provider}")
    a2 = _agent(f"cap0b_{provider}", use_openai=use_openai)
    a2._session_mgr = SessionManager.create(f"cap0b_{provider}")

    for msg, kw in corpus:
        a1._tree_record(msg, required=True, **kw)                      # 现行内联 capture 路径
        for ev in events_from_provider_message(                         # capture-at-emit 路径
                msg, provider=provider, model=a2.model, **kw):
            assert AgentSession(a2).record_event(ev) is True

    a1._session_mgr.close()
    a2._session_mgr.close()
    t1 = _tree_messages(f"cap0a_{provider}")
    t2 = _tree_messages(f"cap0b_{provider}")
    assert t1 == t2 and len(t1) > 0


# ── 事件结构字段投影 ─────────────────────────────────────────────────────────

def test_assistant_event_projection_fields():
    msg, kw = ANTHROPIC_CORPUS[3]
    (ev,) = events_from_provider_message(msg, provider="anthropic", model="claude-x", **kw)
    assert isinstance(ev, AssistantMessageCompleted)
    assert ev.text == "doing it" and ev.thinking == "hmm"
    assert ev.tool_uses == [{"id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}]
    assert ev.stop_reason == "toolUse"                  # 原生 tool_use → 中立 toolUse
    assert ev.usage == {"input_tokens": 7, "output_tokens": 2} and ev.latency_ms == 55
    assert ev.message["stopReason"] == "toolUse"


def test_tool_result_batch_splits_and_projects():
    msg, kw = ANTHROPIC_CORPUS[5]
    evs = events_from_provider_message(msg, provider="anthropic", model="m", **kw)
    assert [type(e) for e in evs] == [ToolResultCompleted, ToolResultCompleted]
    e1, e2 = evs
    assert e1.tool_use_id == "tu_1" and e1.tool == "read_file" and e1.latency_ms == 42
    assert e2.tool_use_id == "tu_2" and e2.is_error is True
    assert e2.latency_ms == 99                          # per-block 缺 → 退回调用级 latency_ms


def test_user_block_content_preserved_via_message_field():
    msg, _ = ANTHROPIC_CORPUS[1]
    (ev,) = events_from_provider_message(msg, provider="anthropic", model="m")
    assert isinstance(ev, UserMessageAccepted)
    assert ev.text == ""                                # block content：text 投影为空
    types = [b["type"] for b in ev.message["content"]]
    assert types == ["text", "image"]                   # 但 neutral message 全保留


def test_openai_system_yields_no_events():
    assert events_from_provider_message({"role": "system", "content": "s"},
                                        provider="openai", model="m") == []


def test_openai_bad_tool_args_keep_audit_raw():
    msg, kw = OPENAI_CORPUS[2]
    (ev,) = events_from_provider_message(msg, provider="openai", model="m", **kw)
    bad = [b for b in ev.message["content"] if b.get("id") == "c2"][0]
    assert bad["arguments"] == {} and bad["argumentsRaw"] == "NOT-JSON{"


# ── record_event required 语义 ───────────────────────────────────────────────

def test_record_event_message_family_fails_loud_without_lease():
    a = _agent("cap0nolease")
    assert a._session_mgr is None
    (ev,) = events_from_provider_message({"role": "user", "content": "x"},
                                         provider="anthropic", model="m")
    with pytest.raises(T.SessionTreeError):
        AgentSession(a).record_event(ev)


def test_record_event_user_text_fallback_without_neutral_message():
    a = _agent("cap0fallback")
    a._session_mgr = SessionManager.create("cap0fallback")
    AgentSession(a).record_event(UserMessageAccepted(text="plain"))
    a._session_mgr.close()
    (m,) = _tree_messages("cap0fallback")
    assert m == {"role": "user", "content": "plain"}
