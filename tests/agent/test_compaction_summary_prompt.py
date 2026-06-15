"""docs/18 Phase 3：结构化 compact summarizer + <analysis> 清理 + prompt-too-long 降级重试。"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.agent.summary_prompts import (
    branch_summary_prompt,
    compact_prompt,
    format_compact_summary,
    partial_compact_prompt,
)
from nanocode.session import tree
from nanocode.session.manager import SessionManager


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


# ── 结构化 prompt 内容 ─────────────────────────────────────────────────────────
def test_compact_prompt_is_no_tools_structured():
    p = compact_prompt()
    assert "Do NOT call any tools" in p
    assert "<analysis>" in p and "<summary>" in p
    for section in ("## Goal", "## Constraints & Preferences", "## Progress",
                    "## Key Decisions", "## Files and Code Sections", "## Errors & Fixes",
                    "## Current Work", "## Next Steps", "## Critical Context",
                    "## read-files", "## modified-files"):
        assert section in p
    # 文件追踪边界：不得把 repo map 当已读事实
    assert "repository map" in p.lower() or "repo map" in p.lower()


def test_compact_prompt_custom_instructions_enter_additional_instructions():
    p = compact_prompt("focus on the API redesign decisions")
    assert "## Additional Instructions" in p
    assert "focus on the API redesign decisions" in p
    # 无自定义指令则无该 section
    assert "## Additional Instructions" not in compact_prompt()


def test_partial_and_branch_prompts_are_no_tools():
    assert "Do NOT call any tools" in partial_compact_prompt()
    assert "PARTIAL view" in partial_compact_prompt()
    bp = branch_summary_prompt()
    assert "Do NOT call any tools" in bp
    assert "## Next Steps" in bp and "## read-files" in bp


# ── <analysis> 清理 ────────────────────────────────────────────────────────────
def test_format_compact_summary_extracts_summary_tag():
    raw = "<analysis>\nscratch reasoning here\n</analysis>\n<summary>\nGoal: do X\n</summary>"
    assert format_compact_summary(raw) == "Goal: do X"


def test_format_compact_summary_strips_analysis_without_summary_tag():
    raw = "<analysis>scratch</analysis>\n\n## Goal\nreal content"
    out = format_compact_summary(raw)
    assert "<analysis>" not in out and "scratch" not in out
    assert out.startswith("## Goal")


def test_format_compact_summary_idempotent_and_safe_on_plain_text():
    assert format_compact_summary("plain summary") == "plain summary"
    assert format_compact_summary("") == ""
    assert format_compact_summary(None) == ""


# ── tree 里的 compaction summary 不含 <analysis> ───────────────────────────────
def test_tree_compaction_summary_has_no_analysis(monkeypatch):
    a = _agent("p3_strip")
    mgr = SessionManager.create("p3_strip")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("old question " * 30))
    mgr.append_message(tree.assistant_message([tree.text_block("a")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(tree.user_message("recent"))

    async def fake(messages=None, instructions=None):
        return "<analysis>internal scratch reasoning</analysis><summary>CLEAN STRUCTURED SUMMARY</summary>"

    monkeypatch.setattr(a, "_compact_anthropic", fake)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact())
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1
    assert comp[0].data["summary"] == "CLEAN STRUCTURED SUMMARY"
    assert "<analysis>" not in comp[0].data["summary"]
    assert "scratch" not in comp[0].data["summary"]


# ── prompt-too-long 降级重试 ───────────────────────────────────────────────────
def _seed_multi_round(mgr, n_rounds=4):
    for i in range(n_rounds):
        mgr.append_message(tree.user_message(f"question {i} " * 20))
        mgr.append_message(tree.assistant_message([tree.text_block(f"answer {i}")],
                           provider="anthropic", api="anthropic", model="claude-x",
                           stop_reason="stop"))
    mgr.append_message(tree.user_message("the latest recent question"))


def test_compact_retries_on_prompt_too_long_then_succeeds(monkeypatch):
    a = _agent("p3_retry")
    mgr = SessionManager.create("p3_retry")
    a._session_mgr = mgr
    _seed_multi_round(mgr, n_rounds=4)
    state = {"calls": 0, "sizes": []}

    async def flaky(messages=None, instructions=None):
        state["calls"] += 1
        state["sizes"].append(len(str(messages)))
        if state["calls"] <= 2:                  # 头两次溢出
            raise RuntimeError("400: prompt is too long: 999999 tokens")
        return "RECOVERED SUMMARY"

    monkeypatch.setattr(a, "_compact_anthropic", flaky)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact())
    assert state["calls"] == 3                    # 初次 + 2 次重试
    # 每次重试丢最旧 round → summarizer 输入单调变小（API round 级，非字符串截断）
    assert state["sizes"][1] < state["sizes"][0]
    assert state["sizes"][2] < state["sizes"][1]
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1 and comp[0].data["summary"] == "RECOVERED SUMMARY"


def test_compact_retry_exhaustion_raises_and_bumps_auto_failure_count(monkeypatch):
    a = _agent("p3_exhaust")
    mgr = SessionManager.create("p3_exhaust")
    a._session_mgr = mgr
    _seed_multi_round(mgr, n_rounds=6)

    async def always_overflow(messages=None, instructions=None):
        raise RuntimeError("prompt is too long")

    monkeypatch.setattr(a, "_compact_anthropic", always_overflow)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)

    # 手动 /compact：重试耗尽后错误可见（上抛）
    with pytest.raises(RuntimeError):
        asyncio.run(a.agent_session.compact())
    assert [e for e in mgr.entries() if e.type == tree.COMPACTION] == []

    # auto 路径：check_and_compact 吞掉异常但失败计数 +1
    a.last_input_token_count = a.effective_window
    asyncio.run(a.agent_session.check_and_compact())
    assert a._consecutive_compaction_failures == 1


def test_non_overflow_summarizer_error_not_retried(monkeypatch):
    a = _agent("p3_nonoverflow")
    mgr = SessionManager.create("p3_nonoverflow")
    a._session_mgr = mgr
    _seed_multi_round(mgr, n_rounds=4)
    state = {"calls": 0}

    async def boom(messages=None, instructions=None):
        state["calls"] += 1
        raise RuntimeError("503 service unavailable")

    monkeypatch.setattr(a, "_compact_anthropic", boom)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    with pytest.raises(RuntimeError):
        asyncio.run(a.agent_session.compact())
    assert state["calls"] == 1                    # 非溢出错误不重试


# ── review HIGH/MED：失败语义（append 失败 / 退化 summary）计入熔断，不当成功 ─────
def test_compaction_append_failure_counts_as_failure(monkeypatch):
    a = _agent("p3_appendfail")
    mgr = SessionManager.create("p3_appendfail")
    a._session_mgr = mgr
    _seed_multi_round(mgr, n_rounds=2)

    async def ok(messages=None, instructions=None):
        return "SUMMARY"

    def boom_append(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(a, "_compact_anthropic", ok)
    monkeypatch.setattr(mgr, "append_compaction", boom_append)   # entry append 失败
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    # 手动：append 失败 → 上抛（不当成功）
    with pytest.raises(Exception):
        asyncio.run(a.agent_session.compact())
    assert [e for e in mgr.entries() if e.type == tree.COMPACTION] == []
    # auto：check_and_compact 捕获并 +1（不清零）
    a.last_input_token_count = a.effective_window
    asyncio.run(a.agent_session.check_and_compact())
    assert a._consecutive_compaction_failures == 1


def test_degenerate_summary_counts_as_failure(monkeypatch):
    # summarizer 返回非空但 format 后为空（只有 <analysis>）→ 失败，不当 no-op（否则无限浪费 LLM 调用）。
    a = _agent("p3_degenerate")
    mgr = SessionManager.create("p3_degenerate")
    a._session_mgr = mgr
    _seed_multi_round(mgr, n_rounds=2)

    async def analysis_only(messages=None, instructions=None):
        return "<analysis>just scratch, no summary tag</analysis>"

    monkeypatch.setattr(a, "_compact_anthropic", analysis_only)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    with pytest.raises(Exception):
        asyncio.run(a.agent_session.compact())
    assert [e for e in mgr.entries() if e.type == tree.COMPACTION] == []
    a.last_input_token_count = a.effective_window
    asyncio.run(a.agent_session.check_and_compact())
    assert a._consecutive_compaction_failures == 1


def test_retry_uses_partial_prompt_flag(monkeypatch):
    # prompt-too-long retry 时 host._summarizer_partial 被置 True（驱动 core 选 partial_compact_prompt）。
    a = _agent("p3_partialflag")
    mgr = SessionManager.create("p3_partialflag")
    a._session_mgr = mgr
    _seed_multi_round(mgr, n_rounds=4)
    seen = []

    async def flaky(messages=None, instructions=None):
        seen.append(getattr(a, "_summarizer_partial", None))
        if len(seen) == 1:
            raise RuntimeError("prompt is too long")
        return "S"

    monkeypatch.setattr(a, "_compact_anthropic", flaky)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact())
    assert seen[0] is False        # 初次：全量 prompt
    assert seen[1] is True         # 重试：partial prompt
