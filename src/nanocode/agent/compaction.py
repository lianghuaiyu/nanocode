"""上下文压缩相关常量与大结果落盘。

当工具结果超过阈值时写入磁盘，并把上下文里的条目替换为简短预览 + 文件路径；
模型可随后用 read_file 取回完整输出，不丢失信息。"""

from __future__ import annotations

import time

from ..paths import tool_results_dir

SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell", "sandbox_shell"}
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIP_THRESHOLD = 0.60
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes
KEEP_RECENT_RESULTS = 3


def persist_large_result(tool_name: str, result: str) -> str:
    THRESHOLD = 30 * 1024  # 30 KB
    if len(result.encode()) <= THRESHOLD:
        return result
    d = tool_results_dir()
    filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
    filepath = d / filename
    filepath.write_text(result, encoding="utf-8")

    lines = result.split("\n")
    preview = "\n".join(lines[:200])
    size_kb = len(result.encode()) / 1024

    return (
        f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
        f"Full output saved to {filepath}. "
        f"You can use read_file to see the full result.]\n\n"
        f"Preview (first 200 lines):\n{preview}"
    )


# ─── Multi-tier in-place 压缩管线（compression facade，P-1 子目标1/2）──────────
#
# 把 budget→snip→microcompact 三层裁剪的**实现细节**从两个 backend mixin 收敛到此处，
# 由 CompressionPipeline 统一持有；backend / engine 只 `prepare()` 一次（每次 API 调用前），
# 不再各自实现 tier 逻辑。逻辑逐字节搬自原 openai_backend / anthropic_backend，仅把对
# self.* 的隐式读取参数化为显式入参——**行为不变**：仍每轮原地裁剪 provider 消息列表，
# 无 LLM 调用、无 UI、无副作用外溢（全 OpenAI 摘要式 _compact_* 仍留在 backend，turn 边界跑）。


def _find_tool_use_by_id(messages: list, tool_use_id: str) -> dict | None:
    """anthropic snip 用：按 tool_use_id 在 assistant 消息里回查工具名/入参。"""
    for msg in messages:
        if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                return {"name": block["name"], "input": block.get("input", {})}
    return None


# ── OpenAI 形态（flat role:tool 消息）──────────────────────────────

def _budget_openai(messages, last_input_token_count, effective_window) -> None:
    utilization = last_input_token_count / effective_window if effective_window else 0
    if utilization < 0.5:
        return
    budget = 15000 if utilization > 0.7 else 30000
    for msg in messages:
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
            keep = (budget - 80) // 2
            msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]


def _snip_openai(messages, last_input_token_count, effective_window) -> None:
    utilization = last_input_token_count / effective_window if effective_window else 0
    if utilization < SNIP_THRESHOLD:
        return
    tool_msgs = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
            tool_msgs.append(i)
    if len(tool_msgs) <= KEEP_RECENT_RESULTS:
        return
    snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
    for i in range(snip_count):
        messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER


def _microcompact_openai(messages, last_api_call_time) -> None:
    if not last_api_call_time or (time.time() - last_api_call_time) < MICROCOMPACT_IDLE_S:
        return
    tool_msgs = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
            tool_msgs.append(i)
    clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
    for i in range(max(0, clear_count)):
        messages[tool_msgs[i]]["content"] = "[Old result cleared]"


# ── Anthropic 形态（nested tool_result blocks）────────────────────

def _budget_anthropic(messages, last_input_token_count, effective_window) -> None:
    utilization = last_input_token_count / effective_window if effective_window else 0
    if utilization < 0.5:
        return
    budget = 15000 if utilization > 0.7 else 30000
    for msg in messages:
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                keep = (budget - 80) // 2
                block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]


def _snip_anthropic(messages, last_input_token_count, effective_window) -> None:
    utilization = last_input_token_count / effective_window if effective_window else 0
    if utilization < SNIP_THRESHOLD:
        return

    results = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] != SNIP_PLACEHOLDER:
                tool_use_id = block.get("tool_use_id")
                tool_info = _find_tool_use_by_id(messages, tool_use_id)
                if tool_info and tool_info["name"] in SNIPPABLE_TOOLS:
                    results.append({"mi": mi, "bi": bi, "name": tool_info["name"], "file_path": tool_info.get("input", {}).get("file_path")})

    if len(results) <= KEEP_RECENT_RESULTS:
        return

    to_snip = set()
    seen_files: dict[str, list[int]] = {}
    for i, r in enumerate(results):
        if r["name"] == "read_file" and r.get("file_path"):
            seen_files.setdefault(r["file_path"], []).append(i)

    for indices in seen_files.values():
        if len(indices) > 1:
            for j in indices[:-1]:
                to_snip.add(j)

    snip_before = len(results) - KEEP_RECENT_RESULTS
    for i in range(snip_before):
        to_snip.add(i)

    for idx in to_snip:
        r = results[idx]
        messages[r["mi"]]["content"][r["bi"]]["content"] = SNIP_PLACEHOLDER


def _microcompact_anthropic(messages, last_api_call_time) -> None:
    if not last_api_call_time or (time.time() - last_api_call_time) < MICROCOMPACT_IDLE_S:
        return
    all_results = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                all_results.append((mi, bi))
    clear_count = len(all_results) - KEEP_RECENT_RESULTS
    for i in range(max(0, clear_count)):
        mi, bi = all_results[i]
        messages[mi]["content"][bi]["content"] = "[Old result cleared]"


class CompressionPipeline:
    """每次 API 调用前对 provider 消息列表跑 budget→snip→microcompact 的 facade。

    behavior-preserving：tier 顺序与判据与解耦前完全一致，仅把 self.* 隐式读取改为显式
    入参。无 LLM 调用、无 UI、无副作用外溢——纯 in-place 裁剪。摘要式全压缩（_compact_*）
    不在此，仍由 backend 在 turn 边界发起。
    """

    @staticmethod
    def prepare_openai(messages, *, last_input_token_count, effective_window, last_api_call_time) -> None:
        _budget_openai(messages, last_input_token_count, effective_window)
        _snip_openai(messages, last_input_token_count, effective_window)
        _microcompact_openai(messages, last_api_call_time)

    @staticmethod
    def prepare_anthropic(messages, *, last_input_token_count, effective_window, last_api_call_time) -> None:
        _budget_anthropic(messages, last_input_token_count, effective_window)
        _snip_anthropic(messages, last_input_token_count, effective_window)
        _microcompact_anthropic(messages, last_api_call_time)

