"""agent/core.py — AgentCore：纯模型循环（pi `Agent` 同位，docs/15 §6 / docs/16 #3c）。

签名收敛：`run_turn(state, cfg: AgentLoopConfig, emit, *, stream_fn) -> list[dict]`。
loop **不触 Agent**：状态进（AgentState 快照 + cfg 注入的宿主能力）、事件出（emit 单出口，
扇出 record_event(树)+UI）、工具经 cfg.execute_tool（allowlist fail-closed 咽喉点在 router）。
每个请求经 cfg.rebuild_snapshot() 从 canonical 树重渲染（docs/13 硬不变量：turn 内绝不内存 push，
plan-mode 的 system prompt 切换/上下文复位经重渲染实时生效）。

B2-a（docs/16）：两条 provider 循环变体合一为**一个 provider-agnostic 的 post-stream 单循环**
（Pi 风格 stream→finalize→execute，严格流完再执行,无重叠）。provider 差异全收敛到 adapter 缝下：
- cfg.to_completion(raw) 把两 provider 的 stream() 原始返回归一成 Completion；
- cfg.tool_result_messages(results) 产出各自 wire 形状的 tool-result 消息（anthropic 单条批量 / openai 逐条）。
工具阶段统一用 OpenAI 的 batch 模型（group_tool_batches：连续 allowed 并发安全调用 → asyncio.gather），
对两 provider 统一适用——故 Anthropic 并发安全工具仍**并行**（去掉的只是旧的「流中并发预启动」纯延迟优化）。

turn shell（lease prologue / user 消息 emit / compaction 门 / turn_end / auto_save）在
AgentSession.run_turn（#3）；本模块只剩这一条统一循环与 summary compaction 的 LLM 调用。
"""

from __future__ import annotations

import asyncio
import json
import time

from .events import (
    AssistantDelta,
    BudgetExceeded,
    LlmRequestPrepared,
    NoticeRaised,
    RetryRaised,
    ToolCallRequested,
    ToolResultObserved,
)
from .loop import AgentLoopConfig, group_tool_batches
from .providers import StreamCallbacks, is_context_overflow_error


class AgentCore:
    """模型循环。无状态（循环态都是 turn-local 局部变量）；provider-agnostic 单循环（B2-a）。"""

    async def run_turn(self, state, cfg: AgentLoopConfig, emit, *, stream_fn) -> list[dict]:
        messages: list = []
        overflow_retried = False        # docs/16 #10：每 turn 至多一次 overflow→compact→重试
        while True:
            if cfg.is_aborted():
                break

            cfg.poll_memory()
            cfg.inject_turn_context()

            proj = cfg.rebuild_snapshot()
            messages = proj.messages
            # docs/17 Phase 2：LlmRequestPrepared 是订阅端派生 spinner 的「起」信号（旧 cfg.sink.spinner_start）。
            emit(LlmRequestPrepared(
                model=cfg.model, message_count=len(messages),
                messages_chars=len(json.dumps(messages, default=str))))
            _t_req = time.time()
            cb = StreamCallbacks(
                text_block=lambda t: emit(AssistantDelta(text=t)),
                thinking_block=lambda t: emit(AssistantDelta(thinking=t)),
                retry=lambda a, m, r: emit(RetryRaised(attempt=a, max_retries=m, reason=r)),
            )
            try:
                raw = await stream_fn(
                    model=cfg.model, system=proj.system, tools=cfg.resolve_tools(),
                    messages=messages, thinking_mode=cfg.thinking_mode, callbacks=cb)
            except Exception as e:
                # docs/16 #10：provider 上下文溢出不再是死 turn——压缩后重试一次
                # （abort 门控：被取消的 turn 不做恢复；重试后仍溢出 → 如实上抛）。
                if (not overflow_retried and not cfg.is_aborted()
                        and is_context_overflow_error(e)):
                    overflow_retried = True
                    emit(NoticeRaised(text="Provider context overflow — compacting and retrying once...",
                                      level="warn"))
                    await cfg.compact()
                    continue
                raise
            _latency_ms = int((time.time() - _t_req) * 1000)

            completion = cfg.to_completion(raw)
            cfg.note_api_call()
            # parity：usage 缺失（completion.usage is None，仅 OpenAI 非合规/mock 可达）时不 add_usage
            # （不动 last_input_token_count）、不落 usage 树键——byte-equivalent 于旧 run_openai_turn 的
            # `if response.get("usage")` 守卫。Anthropic 恒携 usage（tuple）。
            _usage = completion.usage
            if _usage is not None:
                cfg.add_usage(_usage[0], _usage[1])

            cfg.record_provider_messages(
                completion.assistant_message, stop_reason=completion.stop_reason,
                usage=({"inputTokens": _usage[0], "outputTokens": _usage[1]}
                       if _usage is not None else None),
                latency_ms=_latency_ms)              # §7.6②：fail-loud（record_event required）

            if not completion.tool_calls:
                if cfg.inject_follow_up():
                    continue
                break

            cfg.bump_turn()
            budget = cfg.check_budget()
            if budget["exceeded"]:
                # docs/17 Phase 2：BudgetExceeded 事件即人面通知源（订阅端渲染），不再额外 sink.info。
                emit(BudgetExceeded(reason=budget["reason"]))
                break

            # ── post-stream 工具阶段（Pi 风格：流完再执行）──────────────────────
            # Phase 1：解析 + 授权（serial）。emit ToolCallRequested → authorize；收集 checked。
            checked: list[dict] = []
            for tc in completion.tool_calls:
                if cfg.is_aborted():
                    break
                emit(ToolCallRequested(tool=tc["name"], input=tc["input"], tool_use_id=tc["id"]))
                allowed, denial = await cfg.authorize(tc["name"], tc["input"])
                checked.append({"tc": tc, "fn": tc["name"], "inp": tc["input"],
                                "allowed": allowed, "result": denial})

            # Phase 2：分组并执行（连续并发安全工具并行 → asyncio.gather；其余串行）。
            results: list[dict] = []          # [{tool_call_id, name, content, is_error, latency_ms}]
            context_break = False
            for batch in group_tool_batches(checked, is_concurrency_safe=cfg.is_concurrency_safe):
                if context_break or cfg.is_aborted():
                    break

                if batch["concurrent"]:
                    async def _run_safe(item: dict) -> dict:
                        _t0 = time.time()
                        raw_res = await cfg.execute_tool(item["fn"], item["inp"])
                        res = cfg.persist_large_result(item["fn"], raw_res)
                        _lat = int((time.time() - _t0) * 1000)
                        emit(ToolResultObserved(tool=item["fn"], tool_use_id=item["tc"]["id"],
                                                chars=len(res), result=res))
                        return {"tool_call_id": item["tc"]["id"], "name": item["fn"],
                                "content": res, "is_error": False, "latency_ms": _lat}
                    results.extend(await asyncio.gather(*[_run_safe(it) for it in batch["items"]]))
                else:
                    for item in batch["items"]:
                        if not item["allowed"]:
                            results.append({"tool_call_id": item["tc"]["id"], "name": item["fn"],
                                            "content": item["result"], "is_error": True,
                                            "latency_ms": None})
                            continue
                        _t_tool = time.time()
                        raw_res = await cfg.execute_tool(item["fn"], item["inp"])
                        res = cfg.persist_large_result(item["fn"], raw_res)
                        _lat = int((time.time() - _t_tool) * 1000)
                        emit(ToolResultObserved(tool=item["fn"], tool_use_id=item["tc"]["id"],
                                                chars=len(res), result=res))

                        if cfg.consume_context_break():
                            # plan clear-and-execute：leaf 已复位 root——结果作为新分支首条 user 消息落树，
                            # 不能作 toolResult（其 toolCall 已不在新分支上，render 会按 inverse-orphan 清掉）。
                            cfg.record_provider_messages({"role": "user", "content": res})
                            context_break = True
                            break
                        results.append({"tool_call_id": item["tc"]["id"], "name": item["fn"],
                                        "content": res, "is_error": False, "latency_ms": _lat})

            # context_break 时本 turn 的工具结果已被新分支首条 user 消息取代（语义同旧两循环）。
            if not context_break:
                for msg, lat in cfg.tool_result_messages(results):
                    cfg.record_provider_messages(msg, latency_ms=lat)   # §7.6①：tool result 必须落树
                cfg.inject_skill_bodies()
        return messages

    #
    # host-driven（经 Agent._compact_* 委托调用，per-instance monkeypatch 锚）：summarizer 是
    # 一次独立 LLM 调用、不在纯 loop 内；真正的 shrink 是 AgentSession.compact 写的 COMPACTION entry。
    # docs/16 #10：summarizer 只吃 **prefix 投影**（kept-suffix 由 AgentSession 的 keepRecentTokens
    # cut-point 排除）——summary 与 fold 保留区绝不双计同一段内容。

    @classmethod
    def _compact_prompt(cls, instructions: str | None = None, *, partial: bool = False) -> str:
        """docs/18 Phase 3：结构化 no-tools + <analysis>/<summary> prompt（summary_prompts）。
        instructions（来自 /compact [prompt]）进入 Additional Instructions section。partial=True（来自
        prompt-too-long retry 丢弃最旧 round 后）走 partial_compact_prompt——告知模型这是被截断的部分视图。"""
        from .summary_prompts import compact_prompt, partial_compact_prompt
        return (partial_compact_prompt if partial else compact_prompt)(instructions)

    # B1 provider seam：四个 _compact_*/_summarize_* 体已下沉到 ProviderAdapter.summarize（providers.py），
    # Agent._compact_*/_summarize_* 薄 delegator（monkeypatch 锚）直接调 self._provider.summarize。
    # 指令拼接 _with_summary_request 也已下沉到 ProviderAdapter（providers.py），此处只保留共享纯函数
    # _compact_prompt（prompt 选择，仍被 engine.py 的 delegator 经 self._core._compact_prompt 活跃调用）。
    #
    # B2-a：block_to_dict 下沉到 AnthropicAdapter._block_to_dict（providers.py，complete() 内用）。
