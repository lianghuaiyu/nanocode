"""docs/17 Phase 5b：RPC/headless 模式——TUI 客户端化的验收试金石。

证明 agent core 与表现层彻底解耦：用 JSON-lines over stdio 驱动**同一个** RuntimeThread，
事件流逐条 JSON 出 stdout，审批经 stdin 往返。core 一行不改。
"""

import asyncio
import json
import sys
import time

from nanocode.agent.engine import Agent
from nanocode.runtime import AgentRuntime
from nanocode.entrypoints.host import RuntimeHost
from nanocode.entrypoints.rpc import run_rpc_mode
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


class _FakeBlock:
    def __init__(self, type="text", **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 7
    output_tokens = 3


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = _FakeUsage()


class _CapStdout:
    """收集 stdout 的逐行输出（run_rpc_mode 每条事件一行 JSON）。"""
    def __init__(self, out): self._out = out; self._buf = ""
    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._out.append(line)
    def flush(self): pass


class _GatedStdin:
    """按步喂 stdin 行；某步可 gate：阻塞直到 stdout 已出现某子串（模拟外部客户端等事件再应答）。"""
    def __init__(self, steps, out): self._steps = steps; self._i = 0; self._out = out
    def readline(self):
        if self._i >= len(self._steps):
            return ""                                  # EOF
        line, wait_for = self._steps[self._i]; self._i += 1
        if wait_for:
            deadline = time.time() + 3.0
            while time.time() < deadline and not any(wait_for in l for l in list(self._out)):
                time.sleep(0.01)
        return line + "\n"


def _agent(sid, stream):
    a = Agent(api_key="test", session_id=sid, permission_mode="default")
    a._mcp_initialized = True
    a.model = "claude-x"
    a._provider.stream = stream
    return a


def _run(agent, steps, monkeypatch):
    out = []
    monkeypatch.setattr(sys, "stdout", _CapStdout(out))
    monkeypatch.setattr(sys, "stdin", _GatedStdin(steps, out))
    rt = AgentRuntime()
    thread = rt._attach_agent(agent)
    host = RuntimeHost(rt, thread, interactive=False)
    asyncio.run(run_rpc_mode(host))
    # 还原 stdout 后再解析（避免解析期写日志又被捕获）
    return [json.loads(l) for l in out]


def _run_host(host, steps, monkeypatch):
    out = []
    monkeypatch.setattr(sys, "stdout", _CapStdout(out))
    monkeypatch.setattr(sys, "stdin", _GatedStdin(steps, out))
    asyncio.run(run_rpc_mode(host))
    return [json.loads(l) for l in out]


# ─── 验收 1：stdio 驱动一个 turn，事件流出 stdout ──────────────────────────────

def test_rpc_drives_turn_and_streams_events(monkeypatch):
    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        callbacks.text_block("hello from rpc")             # → AssistantDelta(text) → final_response
        return _FakeResp([_FakeBlock("text", text="hello from rpc")])

    a = _agent("rpc1", stream)
    msgs = _run(a, [('{"cmd": "prompt", "text": "hi"}', None),
                    ('{"cmd": "exit"}', "turn_result")], monkeypatch)

    kinds = [m.get("type") for m in msgs]
    for expected in ("user_message_accepted", "llm_request_prepared",
                     "assistant_message_completed", "turn_completed", "turn_result"):
        assert expected in kinds, f"missing {expected} in {kinds}"
    # 事件信封形状（core→client 全部表现通道）
    env = next(m for m in msgs if m["type"] == "assistant_message_completed")
    assert set(env) >= {"thread_id", "session_id", "seq", "type", "event"}
    tr = next(m for m in msgs if m["type"] == "turn_result")
    assert tr["status"] == "completed"
    assert tr["final_response"] == "hello from rpc"        # 从事件流派生（Phase 0 累加器）


# ─── 验收 2：审批经 stdio 往返（ApprovalRequested 出 stdout，approval_response 回 stdin）──

def test_rpc_approval_round_trip(monkeypatch):
    calls = {"n": 0}

    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a = _agent("rpc2", stream)

    # 让该工具走危险确认路径（经 confirm_fn = RPC 注入的审批回调）。
    async def fake_authorize(name, inp):
        ok = await a._confirm_dangerous("rm -rf /tmp/x")
        return (ok, None if ok else "Action denied")
    a._authorize_dispatch = fake_authorize

    msgs = _run(a, [('{"cmd": "prompt", "text": "go"}', None),
                    ('{"cmd": "approval_response", "approved": true}', "approval_request"),
                    ('{"cmd": "exit"}', "turn_result")], monkeypatch)

    kinds = [m.get("type") for m in msgs]
    # codex P1 fix：审批的**可应答**通道是 confirm_fn 自带的 approval_request（带 request_id），
    # 不依赖 ApprovalRequested 事件经订阅流到达（对子 agent 至关重要）。
    req = next(m for m in msgs if m.get("type") == "approval_request")
    assert req["request_id"] and "rm -rf" in req["message"]
    # 批准后工具继续执行 → 有工具结果观测，turn 正常收尾
    assert "tool_result_observed" in kinds
    tr = next(m for m in msgs if m["type"] == "turn_result")
    assert tr["status"] == "completed"


def test_rpc_rejects_overlapping_prompt(monkeypatch):
    """codex P1 fix：turn 运行中（这里卡在等审批）再发 prompt → 被拒，不开第二个并发 turn。"""
    calls = {"n": 0}

    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a = _agent("rpcovl", stream)

    async def fake_authorize(name, inp):
        ok = await a._confirm_dangerous("rm -rf /tmp/x")
        return (ok, None if ok else "Action denied")
    a._authorize_dispatch = fake_authorize

    msgs = _run(a, [('{"cmd": "prompt", "text": "one"}', None),
                    ('{"cmd": "prompt", "text": "two"}', "approval_request"),   # 首 turn 等审批时插第二个
                    ('{"cmd": "approval_response", "approved": true}', "already running"),
                    ('{"cmd": "exit"}', "turn_result")], monkeypatch)

    errs = [m for m in msgs if m.get("type") == "response"
            and m.get("command") == "prompt"
            and m.get("success") is False
            and "already running" in m.get("error", "")]
    assert errs                                              # 第二个 prompt 被拒
    assert len([m for m in msgs if m.get("type") == "turn_result"]) == 1   # 只有一个 turn 跑过


def test_rpc_detached_turn_error_emits_result(monkeypatch):
    """codex P2 fix：detached turn 抛错也要 emit 结构化 turn_result(status=error)，客户端不空等。"""
    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        raise ValueError("provider boom")

    a = _agent("rpcerr", stream)
    msgs = _run(a, [('{"cmd": "prompt", "text": "go"}', None),
                    ('{"cmd": "exit"}', "turn_result")], monkeypatch)
    tr = next(m for m in msgs if m.get("type") == "turn_result")
    assert tr["status"] == "error" and "boom" in (tr.get("error") or "")


def test_rpc_get_state_returns_snapshot(monkeypatch):
    """docs/17 #2：get_state 经 stdio 返回会话快照（Pi get_state 对位）。"""
    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        return _FakeResp([_FakeBlock("text", text="ok")])

    a = _agent("rpcstate", stream)
    msgs = _run(a, [('{"id":"g1","cmd": "get_state"}', None),
                    ('{"cmd": "exit"}', "get_state")], monkeypatch)
    st = next(m for m in msgs if m.get("type") == "response"
              and m.get("command") == "get_state")
    assert st["id"] == "g1"
    assert st["data"]["session_id"] == "rpcstate"
    assert "messages" in st["data"] and "model" in st["data"]


def test_rpc_session_stats_messages_and_name(monkeypatch):
    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        return _FakeResp([_FakeBlock("text", text="ok")])

    a = _agent("rpcstats", stream)
    mgr = SessionManager.create("rpcstats")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("hello"))
    rt = AgentRuntime()
    thread = rt._attach_agent(a)
    host = RuntimeHost(rt, thread, interactive=False)

    msgs = _run_host(host, [
        ('{"id":"m1","type":"get_messages"}', None),
        ('{"id":"s1","type":"get_session_stats"}', "get_messages"),
        ('{"id":"n1","type":"set_session_name","name":"named"}', "get_session_stats"),
        ('{"cmd":"exit"}', "set_session_name"),
    ], monkeypatch)

    by_command = {m.get("command"): m for m in msgs if m.get("type") == "response"}
    assert by_command["get_messages"]["data"]["messages"]
    assert by_command["get_session_stats"]["data"]["session_id"] == "rpcstats"
    assert by_command["set_session_name"]["success"] is True
    assert mgr.name() == "named"


def test_rpc_unknown_command_uses_response_envelope(monkeypatch):
    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        return _FakeResp([_FakeBlock("text", text="ok")])

    a = _agent("rpcunknown", stream)
    msgs = _run(a, [('{"id":"u1","type":"does_not_exist"}', None),
                    ('{"cmd":"exit"}', "does_not_exist")], monkeypatch)
    resp = next(m for m in msgs if m.get("type") == "response"
                and m.get("command") == "does_not_exist")
    assert resp["id"] == "u1"
    assert resp["success"] is False
    assert "unknown" in resp["error"].lower()


def test_rpc_approval_denied_blocks_tool(monkeypatch):
    calls = {"n": 0}

    async def stream(*, model, system, tools, messages, thinking_mode, callbacks):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="ok")])

    a = _agent("rpc3", stream)

    async def fake_authorize(name, inp):
        ok = await a._confirm_dangerous("rm -rf /tmp/x")
        return (ok, None if ok else "Action denied")
    a._authorize_dispatch = fake_authorize

    msgs = _run(a, [('{"cmd": "prompt", "text": "go"}', None),
                    ('{"cmd": "approval_response", "approved": false}', "approval_requested"),
                    ('{"cmd": "exit"}', "turn_result")], monkeypatch)

    tr = next(m for m in msgs if m["type"] == "turn_result")
    assert tr["status"] == "completed"                      # 拒绝后 turn 仍正常收尾（工具被挡）
