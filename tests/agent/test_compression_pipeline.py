"""P-1 子目标(1/2)：CompressionPipeline facade —— budget/snip/microcompact 行为校验。

tier 实现已从 backend mixin 收敛到 compaction.CompressionPipeline。这些测试直接打 facade
（之前 tier 逻辑无直测），锁定 in-place 裁剪语义：阈值门槛、KEEP_RECENT_RESULTS、
SNIP/cleared 幂等标记、OpenAI(flat) 与 Anthropic(nested) 两种消息形态。
"""

from __future__ import annotations

import time

from nanocode.agent.compaction import (
    CompressionPipeline, SNIP_PLACEHOLDER, SNIP_THRESHOLD, KEEP_RECENT_RESULTS,
    MICROCOMPACT_IDLE_S,
)

WIN = 100_000  # effective_window
HIGH = int(WIN * 0.75)   # utilization 0.75 > 0.7 budget, > SNIP_THRESHOLD
LOW = int(WIN * 0.3)     # utilization 0.3 < 0.5 → budget/snip skip


def _oai_tool(content: str) -> dict:
    return {"role": "tool", "tool_call_id": "t", "content": content}


def _anth_result(content: str, tool_use_id: str = "tu") -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}


def _anth_tooluse(name: str, tool_use_id: str = "tu", inp=None) -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": inp or {}}]}


# ─── budget tier ────────────────────────────────────────────

def test_budget_skips_below_half_utilization_openai():
    msgs = [_oai_tool("x" * 50000)]
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=LOW, effective_window=WIN, last_api_call_time=0)
    assert len(msgs[0]["content"]) == 50000  # untouched (utilization < 0.5)


def test_budget_truncates_large_tool_result_openai():
    msgs = [_oai_tool("x" * 50000)]
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=HIGH, effective_window=WIN, last_api_call_time=0)
    # budget=15000 at >0.7; content rewritten to head+marker+tail, shorter than original
    assert len(msgs[0]["content"]) < 50000
    assert "budgeted:" in msgs[0]["content"]


def test_budget_truncates_nested_tool_result_anthropic():
    msgs = [_anth_result("y" * 50000)]
    CompressionPipeline.prepare_anthropic(msgs, last_input_token_count=HIGH, effective_window=WIN, last_api_call_time=0)
    blk = msgs[0]["content"][0]
    assert len(blk["content"]) < 50000 and "budgeted:" in blk["content"]


# ─── snip tier ──────────────────────────────────────────────

def test_snip_keeps_recent_and_snips_old_openai():
    # 5 small tool results, high utilization → snip all but KEEP_RECENT_RESULTS
    msgs = [_oai_tool(f"result-{i}") for i in range(5)]
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=HIGH, effective_window=WIN, last_api_call_time=0)
    snipped = [m for m in msgs if m["content"] == SNIP_PLACEHOLDER]
    kept = [m for m in msgs if m["content"] != SNIP_PLACEHOLDER]
    assert len(snipped) == 5 - KEEP_RECENT_RESULTS
    assert len(kept) == KEEP_RECENT_RESULTS
    # most-recent 3 preserved
    assert msgs[-1]["content"] == "result-4"


def test_snip_respects_snippable_tools_anthropic():
    # interleave tool_use(read_file) + tool_result so _find_tool_use_by_id resolves SNIPPABLE
    msgs = []
    for i in range(5):
        msgs.append(_anth_tooluse("read_file", tool_use_id=f"tu{i}", inp={"file_path": f"/f{i}.py"}))
        msgs.append(_anth_result(f"contents-{i}", tool_use_id=f"tu{i}"))
    CompressionPipeline.prepare_anthropic(msgs, last_input_token_count=HIGH, effective_window=WIN, last_api_call_time=0)
    snipped = sum(
        1 for m in msgs if m.get("role") == "user"
        for b in m["content"] if b.get("content") == SNIP_PLACEHOLDER
    )
    assert snipped == 5 - KEEP_RECENT_RESULTS


def test_snip_skips_below_threshold():
    msgs = [_oai_tool(f"r{i}") for i in range(5)]
    util = int(WIN * (SNIP_THRESHOLD - 0.1))  # below SNIP_THRESHOLD but... ensure < 0.5 too so budget also skips
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=LOW, effective_window=WIN, last_api_call_time=0)
    assert all(m["content"] != SNIP_PLACEHOLDER for m in msgs)


# ─── microcompact tier ──────────────────────────────────────

def test_microcompact_clears_old_when_idle_openai():
    msgs = [_oai_tool(f"r{i}") for i in range(6)]
    # idle: last_api_call_time far in the past → microcompact fires
    old = time.time() - (MICROCOMPACT_IDLE_S + 10)
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=LOW, effective_window=WIN, last_api_call_time=old)
    cleared = [m for m in msgs if m["content"] == "[Old result cleared]"]
    assert len(cleared) == 6 - KEEP_RECENT_RESULTS


def test_microcompact_skips_when_recent():
    msgs = [_oai_tool(f"r{i}") for i in range(6)]
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=LOW, effective_window=WIN, last_api_call_time=time.time())
    assert all(m["content"] != "[Old result cleared]" for m in msgs)


# ─── ordering / idempotency ─────────────────────────────────

def test_tiers_idempotent_markers_not_double_processed():
    # already-snipped entries must not be re-counted; running twice is stable
    msgs = [_oai_tool(f"r{i}") for i in range(5)]
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=HIGH, effective_window=WIN, last_api_call_time=0)
    snapshot = [m["content"] for m in msgs]
    CompressionPipeline.prepare_openai(msgs, last_input_token_count=HIGH, effective_window=WIN, last_api_call_time=0)
    assert [m["content"] for m in msgs] == snapshot  # stable, no further damage
