"""docs/18 Phase 1：CompactionPolicy 阈值 + auto-compaction 失败熔断。

- 纯数学：auto_threshold / manual_blocking_limit / keep_recent_tokens / summary 预留封顶。
- live：连续 auto 失败计数→熔断；成功清零；手动 /compact 绕过熔断；abort 门控。
"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.session import tree
from nanocode.session.compaction_policy import CompactionPolicy
from nanocode.session.manager import SessionManager


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def _seed(mgr):
    mgr.append_message(tree.user_message("old question " * 50))
    mgr.append_message(tree.assistant_message([tree.text_block("a1 " * 20)], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(tree.user_message("recent"))


# ── 纯数学（对齐 Claude Code buffer 语义）──────────────────────────────────────
def test_policy_threshold_math():
    p = CompactionPolicy(effective_window=180_000, summary_output_reserve=20_000)
    assert p.auto_threshold == 180_000 - 20_000 - 13_000
    assert p.manual_blocking_limit == 180_000 - 3_000
    # keep_recent = min(20k, max(4k, 15%*window))；180k*0.15=27k → 封顶 20k
    assert p.keep_recent_tokens() == 20_000
    assert p.max_consecutive_failures == 3


def test_policy_keep_recent_floor_and_cap():
    small = CompactionPolicy(effective_window=10_000, summary_output_reserve=8_000)
    assert small.keep_recent_tokens() == 4_000          # 15%*10k=1500 → 下限 4k
    mid = CompactionPolicy(effective_window=60_000, summary_output_reserve=16_384)
    assert mid.keep_recent_tokens() == 9_000            # 15%*60k=9000 在区间内


def test_policy_for_model_caps_summary_reserve():
    assert CompactionPolicy.for_model(180_000, 64_000).summary_output_reserve == 20_000   # 封顶
    assert CompactionPolicy.for_model(180_000, 16_384).summary_output_reserve == 16_384   # 保守默认透传


# ── live：失败熔断 ─────────────────────────────────────────────────────────────
def test_failure_count_resets_on_success(monkeypatch):
    a = _agent("pol_reset")
    mgr = SessionManager.create("pol_reset")
    a._session_mgr = mgr
    _seed(mgr)
    a._consecutive_compaction_failures = 2          # 预置：之前失败过

    async def ok(messages=None, instructions=None):
        return "SUMMARY"

    monkeypatch.setattr(a, "_compact_anthropic", ok)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    a.last_input_token_count = a.effective_window   # 远超 auto_threshold
    asyncio.run(a.agent_session.check_and_compact())
    assert a._consecutive_compaction_failures == 0  # 成功清零
    assert [e for e in mgr.entries() if e.type == tree.COMPACTION]   # 真的压缩了


def test_breaker_trips_after_three_failures_manual_bypasses(monkeypatch):
    a = _agent("pol_brk")
    mgr = SessionManager.create("pol_brk")
    a._session_mgr = mgr
    _seed(mgr)
    calls = {"auto": 0, "manual": 0}

    async def boom(messages=None, instructions=None):
        if instructions is None:
            calls["auto"] += 1
        else:
            calls["manual"] += 1
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(a, "_compact_anthropic", boom)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    a.last_input_token_count = a.effective_window

    for _ in range(3):                              # 连续 3 次 auto 失败
        asyncio.run(a.agent_session.check_and_compact())
    assert a._consecutive_compaction_failures == 3
    assert calls["auto"] == 3

    asyncio.run(a.agent_session.check_and_compact())   # 第 4 次：熔断 → 跳过
    assert calls["auto"] == 3                       # summarizer 未再被调用

    # 手动 /compact 绕过熔断（直接走 compact，不经 check_and_compact 的门）；其异常照常上抛
    with pytest.raises(RuntimeError):
        asyncio.run(a.agent_session.compact("focus on decisions"))
    assert calls["manual"] == 1


def test_auto_compaction_failure_does_not_crash_turn(monkeypatch):
    # auto 压缩失败被吞（不抛出 check_and_compact），仅计数 + warn——turn 不该因 summarizer 抖动而崩。
    a = _agent("pol_swallow")
    mgr = SessionManager.create("pol_swallow")
    a._session_mgr = mgr
    _seed(mgr)

    async def boom(messages=None, instructions=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(a, "_compact_anthropic", boom)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    a.last_input_token_count = a.effective_window
    asyncio.run(a.agent_session.check_and_compact())     # 不抛
    assert a._consecutive_compaction_failures == 1


def test_abort_gate_blocks_auto_compact(monkeypatch):
    a = _agent("pol_abort")
    mgr = SessionManager.create("pol_abort")
    a._session_mgr = mgr
    _seed(mgr)
    calls = {"n": 0}

    async def fake(messages=None, instructions=None):
        calls["n"] += 1
        return "S"

    monkeypatch.setattr(a, "_compact_anthropic", fake)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    a.last_input_token_count = a.effective_window
    a._aborted = True
    asyncio.run(a.agent_session.check_and_compact())
    assert calls["n"] == 0                          # abort 门控：不压缩
    a._aborted = False
    asyncio.run(a.agent_session.check_and_compact())
    assert calls["n"] == 1


def test_below_threshold_does_not_compact(monkeypatch):
    a = _agent("pol_below")
    mgr = SessionManager.create("pol_below")
    a._session_mgr = mgr
    _seed(mgr)
    calls = {"n": 0}

    async def fake(messages=None, instructions=None):
        calls["n"] += 1
        return "S"

    monkeypatch.setattr(a, "_compact_anthropic", fake)
    a.last_input_token_count = 100                  # 远低于 auto_threshold
    asyncio.run(a.agent_session.check_and_compact())
    assert calls["n"] == 0


def test_clear_history_resets_breaker():
    # review：/clear 起全新逻辑对话 → 复位失败熔断 + tool-result 替换状态（旧分支的失败不应永久熔断新对话）。
    a = _agent("pol_clear")
    mgr = SessionManager.create("pol_clear")
    a._session_mgr = mgr
    a._consecutive_compaction_failures = 3          # 已熔断
    a._content_replacement.seen_ids.add("stale")
    a.agent_session.clear_history()
    assert a._consecutive_compaction_failures == 0
    assert a._content_replacement.seen_ids == set()
