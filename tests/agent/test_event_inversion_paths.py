"""docs/16 #1（STEP D-1）四路径专项：message family 经 capture-at-emit → record_event 唯一树写者。

四条路径分别驱动（fake provider），断言：树 MESSAGE 序列与 cutover 前的内联 _tree_record 语义一致、
emit 顺序 = 原 inline 顺序（user → assistant → toolResult → …）、turn-end `verify_turn_consistency`
零问题（§7.6：inverse-orphan / leaf 漂移 / firstKept 可达）。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.session.agent import AgentSession
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


class _FakeBlock:
    def __init__(self, type="text", **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResp:
    def __init__(self, content, stop_reason=None):
        self.content = content
        self.usage = _FakeUsage()
        if stop_reason is not None:
            self.stop_reason = stop_reason


def _agent(sid, **kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    a = Agent(api_key="test", session_id=sid, **kw)
    a._mcp_initialized = True
    if not a.use_openai:
        a.model = "claude-x"
    return a


def _tree_roles(sid):
    return [e.data["message"]["role"] for e in SessionManager.open(sid).entries()
            if e.type == T.MESSAGE]


def _tree_msgs(sid):
    return [e.data["message"] for e in SessionManager.open(sid).entries() if e.type == T.MESSAGE]


# ── 路径 1：anthropic 串行工具回合 ────────────────────────────────────────────

def test_anthropic_serial_tool_round_tree_and_consistency():
    a = _agent("evt_anth")
    calls = {"n": 0}

    async def fake_stream(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})],
                             stop_reason="tool_use")
        return _FakeResp([_FakeBlock("text", text="done")], stop_reason="end_turn")

    a._provider.stream = fake_stream
    asyncio.run(a.chat("list"))

    assert _tree_roles("evt_anth") == ["user", "assistant", "toolResult", "assistant"]
    msgs = _tree_msgs("evt_anth")
    assert msgs[1]["stopReason"] == "toolUse" and msgs[3]["stopReason"] == "stop"   # 原生→中立映射
    assert msgs[1]["usage"] == {"inputTokens": 10, "outputTokens": 5}
    assert isinstance(msgs[1].get("latencyMs"), int)                                # latency 必带（trajectory 依赖）
    assert msgs[2]["toolCallId"] == "t1" and isinstance(msgs[2].get("latencyMs"), int)
    assert AgentSession(a).verify_turn_consistency() == []


# ── 路径 2：anthropic early-exec（流中并发预执行 CONCURRENCY_SAFE 工具）──────────

def test_anthropic_early_exec_path_tree_and_consistency():
    a = _agent("evt_early")
    calls = {"n": 0}

    async def fake_stream(*, callbacks=None, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            blk = {"id": "e1", "name": "list_files", "input": {"path": "."}}
            if callbacks and callbacks.tool_block:
                callbacks.tool_block(blk)               # 流中触发 early-exec（CONCURRENCY_SAFE + allow）
            return _FakeResp([_FakeBlock("tool_use", id="e1", name="list_files", input={"path": "."})],
                             stop_reason="tool_use")
        return _FakeResp([_FakeBlock("text", text="ok")], stop_reason="end_turn")

    a._provider.stream = fake_stream
    asyncio.run(a.chat("scan"))

    assert _tree_roles("evt_early") == ["user", "assistant", "toolResult", "assistant"]
    tr = _tree_msgs("evt_early")[2]
    assert tr["toolCallId"] == "e1" and tr["toolName"] == "list_files"
    assert isinstance(tr.get("latencyMs"), int)         # early 路径的 per-tool 延迟同样入树
    assert AgentSession(a).verify_turn_consistency() == []


# ── 路径 3：openai 并发 batch（连续 CONCURRENCY_SAFE 工具并行执行）───────────────

def _openai_agent(sid):
    return _agent(sid, api_base="http://localhost:1/v1", model="gpt-test")


def test_openai_concurrent_batch_tree_and_consistency():
    a = _openai_agent("evt_oai_batch")
    calls = {"n": 0}

    async def fake_stream(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"usage": {"prompt_tokens": 9, "completion_tokens": 4},
                    "choices": [{"finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None, "tool_calls": [
                            {"type": "function", "id": "b1",
                             "function": {"name": "list_files", "arguments": '{"path": "."}'}},
                            {"type": "function", "id": "b2",
                             "function": {"name": "list_files", "arguments": '{"path": "."}'}},
                        ]}}]}
        return {"usage": {"prompt_tokens": 3, "completion_tokens": 1},
                "choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": "done"}}]}

    a._provider.stream = fake_stream
    asyncio.run(a.chat("go"))

    roles = _tree_roles("evt_oai_batch")
    assert roles == ["user", "assistant", "toolResult", "toolResult", "assistant"]
    msgs = _tree_msgs("evt_oai_batch")
    assert {msgs[2]["toolCallId"], msgs[3]["toolCallId"]} == {"b1", "b2"}          # 并发批的两条都落树
    assert msgs[1]["stopReason"] == "toolUse" and msgs[4]["stopReason"] == "stop"
    assert AgentSession(a).verify_turn_consistency() == []


# ── 路径 4：openai denied（授权拒绝也必须作为 toolResult 落树）────────────────────

def test_openai_denied_tool_result_lands_in_tree(monkeypatch):
    a = _openai_agent("evt_oai_deny")
    calls = {"n": 0}

    async def deny_all(name, inp):
        return False, f"Error: '{name}' denied by test"

    monkeypatch.setattr(a, "_authorize_dispatch", deny_all)

    async def fake_stream(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"usage": {"prompt_tokens": 2, "completion_tokens": 2},
                    "choices": [{"finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None, "tool_calls": [
                            {"type": "function", "id": "d1",
                             "function": {"name": "run_shell", "arguments": '{"command": "ls"}'}},
                        ]}}]}
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}]}

    a._provider.stream = fake_stream
    asyncio.run(a.chat("try"))

    msgs = _tree_msgs("evt_oai_deny")
    assert [m["role"] for m in msgs] == ["user", "assistant", "toolResult", "assistant"]
    assert msgs[2]["toolCallId"] == "d1" and "denied by test" in str(msgs[2]["content"])
    assert AgentSession(a).verify_turn_consistency() == []


# ── required 写失败 fail-loud（record_event 路径，对照旧 _tree_record 同语义）────

def test_record_event_write_failure_fails_loud(monkeypatch):
    import pytest
    from nanocode.session.lease import SessionLease
    a = _agent("evt_reqw")
    a._session_mgr = SessionLease.open_or_create("evt_reqw").manager

    def boom(*args, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(a._session_mgr, "append_message", boom)
    from nanocode.agent.events import events_from_provider_message
    (ev,) = events_from_provider_message({"role": "user", "content": "hi"},
                                         provider="anthropic", model="m")
    with pytest.raises(OSError):
        AgentSession(a).record_event(ev)               # required 写失败 → 重抛，绝不静默丢上下文
