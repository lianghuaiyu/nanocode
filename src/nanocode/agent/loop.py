"""agent/loop.py — provider-independent 循环辅助 + AgentLoopConfig（docs/15 §5 / docs/16 #3c）。

把两条后端循环里可纯函数化的小块抽出来（post-stream 的 serial-check → parallel-batch 分组,对两
provider 统一适用）,使 AgentCore 的循环更薄、可单测。纯函数,无 I/O、无 self。

AgentLoopConfig：AgentCore.run_turn 的全部宿主能力注入面（docs/16 #3c）。loop 不再触 Agent——
它只见 (state, cfg, emit, stream_fn)。cfg 的 callable 字段由 AgentSession._loop_config 绑定到
宿主（execute_tool=router.dispatch 入口、rebuild_snapshot=树渲染、authorize/budget/计数 writeback、
注入器、turn-scoped context-break 信号）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..tools import CONCURRENCY_SAFE_TOOLS


@dataclass
class AgentLoopConfig:
    """一个 turn 的模型循环配置：scalars + 宿主能力注入（docs/16 #3c）。

    安全不变量：execute_tool 必须是 allowlist fail-closed 咽喉点（router.dispatch）所在的入口；
    authorize（validate→gate→confirm 完整授权）是 post-stream 工具阶段唯一的授权路径。
    """

    # ── scalars（turn 开始快照；plan-mode 的 system prompt 切换经 rebuild_snapshot 实时生效）──
    # B2-a：provider 派发已删（单一 provider-agnostic 循环）；wire/capture 形状由 adapter 决定，
    # 循环不再读 provider，故不在 config 上保留该死字段。
    model: str
    thinking_mode: str
    is_sub_agent: bool
    # active-tool 解析器（G2）：每请求调用,读 agent registry 当前激活集（tool_search 可在 turn 内
    # 激活工具,故不是 turn-start 快照）。过滤逻辑 get_active_tool_definitions 由 ②b 在 _loop_config
    # 绑定（① adapter 不再 import ③ tools），① 收到的即最终请求工具表,直接发往 provider。
    resolve_tools: Callable[[], list]
    # ── 请求构建 / 完成归一 / 消息落树 ──
    rebuild_snapshot: Callable[[], Any]               # () -> ProviderProjection（每请求树渲染）
    to_completion: Callable[[Any], Any]               # (raw stream() 返回) -> Completion（B2-a：provider 归一）
    record_provider_messages: Callable[..., None]     # (provider_msg, **kw) -> None（capture-at-emit）
    tool_result_messages: Callable[[list], list]      # (results) -> [(provider_msg, latency_ms), ...]（B2-a）
    # ── 工具派发 ──
    execute_tool: Callable[[str, dict], Awaitable[str]]
    authorize: Callable[[str, dict], Awaitable[tuple]]
    persist_large_result: Callable[[str, str], str]
    # ── 预算 / 计数 writeback ──
    check_budget: Callable[[], dict]
    bump_turn: Callable[[], None]
    note_api_call: Callable[[], None]
    add_usage: Callable[[int, int], None]
    token_totals: Callable[[], tuple]
    # ── 控制流信号 ──
    is_aborted: Callable[[], bool]
    compact: Callable[[], Awaitable[None]]            # overflow 恢复（docs/16 #10）：压缩后重试一次
    consume_context_break: Callable[[], bool]         # plan clear-and-execute 的 turn 内信号
    # ── turn-boundary 注入 ──
    inject_turn_context: Callable[[], None]           # finished_tasks + skill_listing
    inject_follow_up: Callable[[], bool]              # child follow-up steer at natural stop
    inject_skill_bodies: Callable[[], None]
    poll_memory: Callable[[], None]                   # memory prefetch settle → ContextInjected


def group_tool_batches(checked: list[dict]) -> list[dict]:
    """post-stream Phase 2 grouping（B2-a，对两 provider 统一适用）：把 serial-checked 的 tool calls
    分组——连续的 allowed + concurrency-safe 工具并成一个并行 batch,其余各自成串行 batch
    （移植自旧 openai loop 的 group_openai_batches,逻辑逐字一致；仅更名以示 provider-agnostic）。

    入参 checked：[{tc, fn, inp, allowed, result?}, ...]（serial 权限判定后的结果）。
    返回：[{concurrent: bool, items: [...]}, ...]。
    """
    batches: list[dict] = []
    for ct in checked:
        safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
        if safe and batches and batches[-1]["concurrent"]:
            batches[-1]["items"].append(ct)
        else:
            batches.append({"concurrent": safe, "items": [ct]})
    return batches
