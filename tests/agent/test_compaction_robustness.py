"""docs/16 #10：compaction 触发健壮化——overflow 恢复 + abort 门控。

- provider 上下文溢出不再是死 turn：compact 一次 + 重试一次；二次溢出如实上抛；
- 被取消的 turn 不做 overflow 恢复、auto-compact 阈值门对 aborted 直接短路。
"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.agent.providers import is_context_overflow_error


class _FakeBlock:
    def __init__(self, type="text", **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = _FakeUsage()


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def test_overflow_detector_matches_provider_messages():
    assert is_context_overflow_error(Exception("400: prompt is too long: 250000 tokens"))
    assert is_context_overflow_error(Exception("This model's maximum context length is 128000"))
    assert is_context_overflow_error(Exception("code: context_length_exceeded"))
    assert not is_context_overflow_error(Exception("401 Unauthorized"))
    assert not is_context_overflow_error(Exception("rate_limit_error"))


def test_overflow_compacts_and_retries_once():
    a = _agent("ovf1")
    calls = {"stream": 0, "compact": 0}

    async def fake_stream(**_kw):
        calls["stream"] += 1
        if calls["stream"] == 1:
            raise RuntimeError("400: prompt is too long: 999999 tokens > 200000 maximum")
        return _FakeResp([_FakeBlock("text", text="recovered")])

    async def fake_compact(instructions=None):
        calls["compact"] += 1

    a._provider.stream = fake_stream
    a.agent_session.compact = fake_compact
    asyncio.run(a.chat("hello"))
    assert calls == {"stream": 2, "compact": 1}     # 压缩一次 + 重试成功，turn 正常收尾


def test_overflow_twice_propagates():
    a = _agent("ovf2")
    calls = {"compact": 0}

    async def always_overflow(**_kw):
        raise RuntimeError("prompt is too long")

    async def fake_compact(instructions=None):
        calls["compact"] += 1

    a._provider.stream = always_overflow
    a.agent_session.compact = fake_compact
    with pytest.raises(RuntimeError):
        asyncio.run(a.chat("hello"))
    assert calls["compact"] == 1                    # 每 turn 至多恢复一次，二次如实上抛


def test_non_overflow_errors_do_not_trigger_compaction():
    a = _agent("ovf3")
    calls = {"compact": 0}

    async def boom(**_kw):
        raise RuntimeError("503 service unavailable")

    async def fake_compact(instructions=None):
        calls["compact"] += 1

    a._provider.stream = boom
    a.agent_session.compact = fake_compact
    with pytest.raises(RuntimeError):
        asyncio.run(a.chat("hello"))
    assert calls["compact"] == 0


def test_check_and_compact_gated_when_aborted():
    a = _agent("ovf4")
    calls = {"compact": 0}

    async def fake_compact(instructions=None):
        calls["compact"] += 1

    a.agent_session.compact = fake_compact
    a.last_input_token_count = a.effective_window      # 远超 0.85 阈值
    a._aborted = True
    asyncio.run(a.agent_session.check_and_compact())
    assert calls["compact"] == 0                       # abort 门控：不压缩
    a._aborted = False
    asyncio.run(a.agent_session.check_and_compact())
    assert calls["compact"] == 1                       # 非 abort 正常触发
