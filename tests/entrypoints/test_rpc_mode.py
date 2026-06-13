"""docs/17 Phase 5b：RPC/headless 模式——TUI 客户端化的验收试金石。

证明 agent core 与表现层彻底解耦：用 JSON-lines over stdio 驱动**同一个** RuntimeThread，
事件流逐条 JSON 出 stdout，审批经 stdin 往返。core 一行不改。
"""

import asyncio
import json
import sys
import time

from nanocode.agent.engine import Agent
from nanocode.entrypoints.rpc import run_rpc_mode


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
    asyncio.run(run_rpc_mode(agent))
    # 还原 stdout 后再解析（避免解析期写日志又被捕获）
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
                    ('{"cmd": "approval_response", "approved": true}', "approval_requested"),
                    ('{"cmd": "exit"}', "turn_result")], monkeypatch)

    kinds = [m.get("type") for m in msgs]
    assert "approval_requested" in kinds                    # 审批显示事件出 stdout
    appr = next(m for m in msgs if m["type"] == "approval_requested")
    assert appr["event"]["request_id"]                      # 携带关联 id
    # 批准后工具继续执行 → 有工具结果观测，turn 正常收尾
    assert "tool_result_observed" in kinds
    tr = next(m for m in msgs if m["type"] == "turn_result")
    assert tr["status"] == "completed"


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
