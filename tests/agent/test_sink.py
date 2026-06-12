"""P-1 解耦目标(3)：EventSink 注入边界——core 不直接 import ..ui，可在无 UI 下跑 turn。"""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.sink import EventSink, TerminalSink, BufferSink, NullSink
from nanocode.agents import registry as config


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="sinksid", **kw)


def _read_only_sub(parent):
    profile = config.build_profile("explore")
    return parent._build_sub_agent(
        system_prompt=profile.prompt, tools=config.effective_tools(profile), agent_type="explore")


class RecordingSink(NullSink):
    """记录所有事件（name, args）的 sink，用于断言 core 经 sink 发事件而非直达 UI。"""

    def __init__(self):
        self.events: list[tuple] = []

    def assistant_markdown(self, text): self.events.append(("assistant_markdown", text))
    def tool_call(self, name, inp): self.events.append(("tool_call", name))
    def tool_result(self, name, result): self.events.append(("tool_result", name))
    def info(self, message): self.events.append(("info", message))
    def sub_agent_start(self, agent_type, description): self.events.append(("sub_agent_start", agent_type))
    def sub_agent_end(self, agent_type, description): self.events.append(("sub_agent_end", agent_type))
    def retry(self, attempt, max_retries, reason): self.events.append(("retry", reason))


# ─── sink 选择 ──────────────────────────────────────────────

def test_main_agent_defaults_to_terminal_sink():
    a = _agent()
    assert isinstance(a._sink, TerminalSink)


def test_sub_agent_defaults_to_buffer_sink():
    sub = _read_only_sub(_agent())
    assert isinstance(sub._sink, BufferSink)


def test_explicit_sink_wins_over_default():
    rec = RecordingSink()
    a = _agent(sink=rec)
    assert a._sink is rec


def test_protocol_conformance():
    # TerminalSink / NullSink / BufferSink 都满足 EventSink 协议
    assert isinstance(TerminalSink(), EventSink)
    assert isinstance(NullSink(), EventSink)
    assert isinstance(BufferSink(), EventSink)


# ─── BufferSink 捕获（取代旧 _output_buffer）──────────────────

def test_emit_block_captured_by_buffer_sink():
    sub = _read_only_sub(_agent())
    sub._emit_block("hello ")
    sub._emit_block("world")
    assert sub._sink.text() == "hello world"
    assert sub._captured_text() == "hello world"


def test_parent_reads_subagent_partial_via_buffer_sink():
    parent = _agent()
    sub = _read_only_sub(parent)
    sub._emit_block("partial output")
    # 父经 sink 读子的 partial（取代旧 getattr(sub, "_output_buffer")）
    assert parent._subagent_captured_text(sub) == "partial output"
    assert parent._subagent_captured_text(None) == ""


# ─── 无 UI 下跑一个 fake turn（criterion 5）─────────────────

def test_fake_turn_without_ui_via_buffer_sink():
    """run_once 在无 UI、无 API 下完成一轮：fake chat 经 _emit_block 写入 BufferSink，
    run_once 从 sink 取回文本。证明 core 不依赖终端即可跑 turn。"""
    sub = _read_only_sub(_agent())

    async def fake_chat(prompt):
        sub._emit_block(f"answer to: {prompt}")

    sub.chat = fake_chat  # 替换真实 LLM 轮次
    result = asyncio.run(sub.run_once("hi"))
    assert result["text"] == "answer to: hi"
    assert "tokens" in result


def test_core_is_headless_with_null_sink():
    """主 agent 注入 NullSink：sink 路由的调用全部无输出、不抛、不碰 UI。"""
    a = _agent(sink=NullSink())
    a._emit_block("x")
    a._sink.tool_call("read_file", {})
    a._sink.tool_result("read_file", "...")
    a._sink.info("note")
    a._sink.spinner_start(); a._sink.spinner_stop()
    a._sink.cost(1, 2)
    assert a._captured_text() == ""  # NullSink 无 text()


# ─── 事件经 sink 发出，而非直达 UI ──────────────────────────

def test_routed_events_land_in_injected_sink():
    rec = RecordingSink()
    a = _agent(sink=rec)
    a._emit_block("md")
    a._sink.tool_call("grep_search", {"pattern": "x"})
    a._sink.sub_agent_start("explore", "desc")
    a._sink.sub_agent_end("explore", "desc")
    kinds = [e[0] for e in rec.events]
    assert kinds == ["assistant_markdown", "tool_call", "sub_agent_start", "sub_agent_end"]


def test_permission_deny_routes_info_through_sink():
    """_authorize_dispatch 的 'Denied: ...' 经 self._sink.info，不再直达 ..ui。"""
    rec = RecordingSink()
    a = _agent(sink=rec, permission_mode="plan")  # plan 模式下 run_shell 被策略拒
    allowed, denial = asyncio.run(a._authorize_dispatch("run_shell", {"command": "ls"}))
    assert allowed is False
    assert any(e[0] == "info" and "Denied" in e[1] for e in rec.events)


def test_buffer_sink_reset_clears_capture():
    b = BufferSink()
    b.assistant_markdown("one")
    assert b.text() == "one"
    b.reset()
    assert b.text() == ""


def test_run_once_resets_capture_per_invocation():
    """Codex review P2：复用的子 agent 实例多次 run_once 不得累积上一轮文本。

    旧 run_once 入口 `_output_buffer = []` 每轮重置；新实现经 BufferSink.reset() 复刻之。
    """
    sub = _read_only_sub(_agent())

    async def fake_chat(prompt):
        sub._emit_block(f"run:{prompt}")

    sub.chat = fake_chat
    r1 = asyncio.run(sub.run_once("a"))
    r2 = asyncio.run(sub.run_once("b"))
    assert r1["text"] == "run:a"
    assert r2["text"] == "run:b"   # 不是 "run:arun:b"——上一轮已重置
