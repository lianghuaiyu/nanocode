"""P4：AgentRuntime / RuntimeThread / TurnResult / ApprovalManager（in-process facade）。

重点是不可回归契约：取消经 abort()、status 在 await 后读 _aborted 映射、final_response
经 TeeSink 捕获不碍打印、ApprovalManager 装两条审批通道。
"""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.runtime import AgentRuntime, RuntimeThread, TurnResult, ApprovalManager, AgentConfig
from nanocode.agent.sink import TeeSink, BufferSink, TerminalSink


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", trace_enabled=False, session_id="p4sid", **kw)


def test_agent_config_build_agent_applies_fields():
    cfg = AgentConfig(api_key="test", model="claude-x", permission_mode="bypassPermissions",
                      max_turns=7, session_id="cfgsid", trace_enabled=False)
    a = cfg.build_agent()
    assert isinstance(a, Agent)
    assert a.model == "claude-x" and a.session_id == "cfgsid"
    assert a.max_turns == 7 and a.permission_mode == "bypassPermissions"
    assert a.use_openai is False                      # 无 api_base → anthropic


def test_agent_config_api_base_selects_openai():
    cfg = AgentConfig(api_key="test", api_base="https://x/v1", session_id="cfgoa",
                      permission_mode="bypassPermissions", trace_enabled=False)
    a = cfg.build_agent()
    assert a.use_openai is True


def test_thread_start_builds_and_registers_thread():
    rt = AgentRuntime()
    cfg = AgentConfig(api_key="test", session_id="tsid", permission_mode="bypassPermissions",
                      trace_enabled=False)
    th = rt.thread_start(cfg)
    assert isinstance(th, RuntimeThread)
    assert th.thread_id == "tsid"
    assert rt.thread("tsid") is th
    assert th.agent.model == cfg.model



def test_adopt_returns_thread_and_registers():
    rt = AgentRuntime()
    a = _agent()
    th = rt.adopt(a)
    assert isinstance(th, RuntimeThread)
    assert th.thread_id == a.session_id
    assert rt.thread(a.session_id) is th
    assert th in rt.threads()


def test_run_returns_turnresult_completed_with_tokens():
    rt = AgentRuntime()
    a = _agent()

    async def fake_chat(prompt):
        a.total_input_tokens += 10
        a.total_output_tokens += 4

    a.chat = fake_chat
    th = rt.adopt(a)
    res = asyncio.run(th.run("hi"))
    assert isinstance(res, TurnResult)
    assert res.status == "completed"
    assert res.input_tokens == 10 and res.output_tokens == 4
    assert res.thread_id == a.session_id


def test_run_maps_aborted_to_cancelled_status():
    """chat() 把取消吞成 _aborted=True 并正常返回——run() 必须在 await 后读 _aborted。"""
    rt = AgentRuntime()
    a = _agent()

    async def aborted_chat(prompt):
        a._aborted = True   # 模拟取消被吞

    a.chat = aborted_chat
    th = rt.adopt(a)
    res = asyncio.run(th.run("x"))
    assert res.status == "cancelled"   # 不是 completed


def test_cancel_delegates_to_abort_order():
    """cancel 必须委托 agent.abort()（先置 _aborted 再 cancel task），不可只 cancel task。"""
    rt = AgentRuntime()
    a = _agent()
    calls = []
    a.abort = lambda: calls.append("abort")
    th = rt.adopt(a)
    th.cancel()
    assert calls == ["abort"]


def test_capture_response_via_tee_preserves_display():
    """capture_response 经 TeeSink：助手文本既进显示 sink 又被捕获到 final_response。"""
    rt = AgentRuntime()
    a = _agent()
    orig_sink = a._sink

    async def fake_chat(prompt):
        a._emit_block("hello world")

    a.chat = fake_chat
    th = rt.adopt(a, capture_response=True)
    assert isinstance(a._sink, TeeSink)        # sink 被外挂为 tee
    res = asyncio.run(th.run("q"))
    assert res.final_response == "hello world"  # 捕获到
    # 原显示 sink 仍在 tee 内（不回归打印）
    assert orig_sink in a._sink._sinks


def test_capture_resets_between_turns():
    rt = AgentRuntime()
    a = _agent()
    seq = iter(["first", "second"])

    async def fake_chat(prompt):
        a._emit_block(next(seq))

    a.chat = fake_chat
    th = rt.adopt(a, capture_response=True)
    r1 = asyncio.run(th.run("a"))
    r2 = asyncio.run(th.run("b"))
    assert r1.final_response == "first"
    assert r2.final_response == "second"   # 不累积


def test_approval_manager_attaches_both_channels():
    a = _agent()

    async def cf(msg): return True
    async def pf(msg): return {"choice": "execute"}

    mgr = ApprovalManager(confirm_fn=cf, plan_approval_fn=pf)
    AgentRuntime().adopt(a, approvals=mgr)
    assert a.confirm_fn is cf
    assert a._plan_approval_fn is pf


def test_approval_manager_none_leaves_defaults():
    a = _agent()
    before_confirm = a.confirm_fn
    AgentRuntime().adopt(a, approvals=ApprovalManager())  # both None
    assert a.confirm_fn is before_confirm   # 不动默认（None→阻塞 input 回退）
