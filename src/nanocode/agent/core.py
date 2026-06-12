"""agent/core.py — AgentCore：纯模型循环（pi `Agent` 同位，docs/15 §6 / docs/16 #3c）。

签名收敛：`run_turn(state, cfg: AgentLoopConfig, emit, *, stream_fn) -> list[dict]`。
loop **不触 Agent**：状态进（AgentState 快照 + cfg 注入的宿主能力）、事件出（emit 单出口，
扇出 record_event(树)+UI）、工具经 cfg.execute_tool（allowlist fail-closed 咽喉点在 router）。
每个请求经 cfg.rebuild_snapshot() 从 canonical 树重渲染（docs/13 硬不变量：turn 内绝不内存 push，
plan-mode 的 system prompt 切换/上下文复位经重渲染实时生效）。

turn shell（lease prologue / user 消息 emit / compaction 门 / turn_end / auto_save）在
AgentSession.run_turn（#3）；本模块只剩两条 provider 循环变体（adapter-driven，不强行合一）
与 summary compaction 的 LLM 调用。
"""

from __future__ import annotations

import asyncio
import json
import time

from ..tools import CONCURRENCY_SAFE_TOOLS
from .events import (
    AssistantDelta,
    BudgetExceeded,
    LlmRequestPrepared,
    ToolCallRequested,
    ToolResultObserved,
)
from .loop import AgentLoopConfig, group_openai_batches
from .providers import StreamCallbacks, is_context_overflow_error


class AgentCore:
    """模型循环。无状态（循环态都是 turn-local 局部变量）；按 cfg.provider 分派循环变体。"""

    async def run_turn(self, state, cfg: AgentLoopConfig, emit, *, stream_fn) -> list[dict]:
        if cfg.provider == "openai":
            return await self.run_openai_turn(state, cfg, emit, stream_fn=stream_fn)
        return await self.run_anthropic_turn(state, cfg, emit, stream_fn=stream_fn)

    # ─── Anthropic turn ──────────────────────────────────────────────────────
    async def run_anthropic_turn(self, state, cfg: AgentLoopConfig, emit, *, stream_fn) -> list[dict]:
        messages: list = []
        overflow_retried = False        # docs/16 #10：每 turn 至多一次 overflow→compact→重试
        while True:
            if cfg.is_aborted():
                break

            cfg.poll_memory()
            cfg.inject_turn_context()

            if not cfg.is_sub_agent:
                cfg.sink.spinner_start()

            early_executions: dict[str, asyncio.Task] = {}
            early_started: dict[str, float] = {}

            def _on_tool_block(block: dict):
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    if cfg.permission_check(block["name"], block["input"]).action == "allow":
                        early_started[block["id"]] = time.time()
                        task = asyncio.create_task(cfg.execute_tool(block["name"], block["input"]))
                        early_executions[block["id"]] = task

            proj = cfg.rebuild_snapshot()
            messages = proj.messages
            emit(LlmRequestPrepared(
                model=cfg.model, message_count=len(messages),
                messages_chars=len(json.dumps(messages, default=str))))
            _t_req = time.time()
            cb = StreamCallbacks(
                spinner_stop=cfg.sink.spinner_stop,
                text_block=lambda t: emit(AssistantDelta(text=t)),
                thinking_block=lambda t: emit(AssistantDelta(thinking=t)),
                tool_block=_on_tool_block,
                retry=cfg.sink.retry,
            )
            try:
                response = await stream_fn(
                    model=cfg.model, system=proj.system, tools=cfg.tools,
                    messages=messages, thinking_mode=cfg.thinking_mode, callbacks=cb)
            except Exception as e:
                # docs/16 #10：provider 上下文溢出不再是死 turn——压缩后重试一次
                # （abort 门控：被取消的 turn 不做恢复；重试后仍溢出 → 如实上抛）。
                if (not overflow_retried and not cfg.is_aborted()
                        and is_context_overflow_error(e)):
                    overflow_retried = True
                    if not cfg.is_sub_agent:
                        cfg.sink.spinner_stop()
                    cfg.sink.info("Provider context overflow — compacting and retrying once...")
                    await cfg.compact()
                    continue
                raise
            _latency_ms = int((time.time() - _t_req) * 1000)

            if not cfg.is_sub_agent:
                cfg.sink.spinner_stop()

            cfg.note_api_call()
            cfg.add_usage(response.usage.input_tokens, response.usage.output_tokens)

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            assistant_msg = {
                "role": "assistant",
                "content": [self.block_to_dict(b) for b in response.content],
            }
            cfg.record_provider_messages(
                assistant_msg, stop_reason=getattr(response, "stop_reason", None),
                usage={"inputTokens": response.usage.input_tokens,
                       "outputTokens": response.usage.output_tokens},
                latency_ms=_latency_ms)              # §7.6②：fail-loud（record_event required）

            if not tool_uses:
                if not cfg.is_sub_agent:
                    cfg.sink.cost(*cfg.token_totals())
                break

            cfg.bump_turn()
            budget = cfg.check_budget()
            if budget["exceeded"]:
                cfg.sink.info(f"Budget exceeded: {budget['reason']}")
                emit(BudgetExceeded(reason=budget["reason"]))
                break

            tool_results: list[dict] = []
            context_break = False
            for tu in tool_uses:
                if context_break or cfg.is_aborted():
                    break
                inp = dict(tu.input) if hasattr(tu.input, "items") else tu.input
                emit(ToolCallRequested(tool=tu.name, input=inp, tool_use_id=tu.id))

                early_task = early_executions.get(tu.id)
                if early_task:
                    raw = await early_task
                    res = cfg.persist_large_result(tu.name, raw)
                    _lat = int((time.time() - early_started.get(tu.id, time.time())) * 1000)
                    emit(ToolResultObserved(tool=tu.name, tool_use_id=tu.id, chars=len(res), result=res))
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res,
                                         "toolName": tu.name, "toolLatencyMs": _lat})
                    continue

                allowed, denial = await cfg.authorize(tu.name, inp)
                if not allowed:
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": denial,
                                         "toolName": tu.name, "is_error": True})
                    continue

                _t_tool = time.time()
                raw = await cfg.execute_tool(tu.name, inp)
                res = cfg.persist_large_result(tu.name, raw)
                _lat = int((time.time() - _t_tool) * 1000)
                emit(ToolResultObserved(tool=tu.name, tool_use_id=tu.id, chars=len(res), result=res))

                if cfg.consume_context_break():
                    # plan clear-and-execute：leaf 已复位 root——结果作为新分支首条 user 消息落树，
                    # 不能作 toolResult（其 toolCall 已不在新分支上，render 会按 inverse-orphan 清掉）。
                    cfg.record_provider_messages({"role": "user", "content": res})
                    context_break = True
                    break
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res,
                                     "toolName": tu.name, "toolLatencyMs": _lat})

            if not context_break and tool_results:
                tr_msg = {"role": "user", "content": tool_results}
                cfg.record_provider_messages(tr_msg)   # §7.6①：tool result 必须落树（否则 toolCall 孤儿）
            if not context_break:
                cfg.inject_skill_bodies()
        return messages

    # ─── OpenAI turn ─────────────────────────────────────────────────────────
    async def run_openai_turn(self, state, cfg: AgentLoopConfig, emit, *, stream_fn) -> list[dict]:
        messages: list = []
        overflow_retried = False        # docs/16 #10：每 turn 至多一次 overflow→compact→重试
        while True:
            if cfg.is_aborted():
                break

            cfg.poll_memory()
            cfg.inject_turn_context()

            if not cfg.is_sub_agent:
                cfg.sink.spinner_start()

            proj = cfg.rebuild_snapshot()
            messages = proj.messages
            emit(LlmRequestPrepared(
                model=cfg.model, message_count=len(messages),
                messages_chars=len(json.dumps(messages, default=str))))
            _t_req = time.time()
            cb = StreamCallbacks(spinner_stop=cfg.sink.spinner_stop,
                                 text_block=lambda t: emit(AssistantDelta(text=t)),
                                 retry=cfg.sink.retry)
            try:
                response = await stream_fn(
                    model=cfg.model, system=None, tools=cfg.tools,
                    messages=messages, thinking_mode=cfg.thinking_mode, callbacks=cb)
            except Exception as e:
                if (not overflow_retried and not cfg.is_aborted()
                        and is_context_overflow_error(e)):
                    overflow_retried = True
                    if not cfg.is_sub_agent:
                        cfg.sink.spinner_stop()
                    cfg.sink.info("Provider context overflow — compacting and retrying once...")
                    await cfg.compact()
                    continue
                raise
            _latency_ms = int((time.time() - _t_req) * 1000)

            if not cfg.is_sub_agent:
                cfg.sink.spinner_stop()

            cfg.note_api_call()
            if response.get("usage"):
                cfg.add_usage(response["usage"]["prompt_tokens"],
                              response["usage"]["completion_tokens"])

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            cfg.record_provider_messages(
                message, stop_reason=choice.get("finish_reason"),
                usage={"inputTokens": _u.get("prompt_tokens", 0),
                       "outputTokens": _u.get("completion_tokens", 0)}
                if (_u := (response.get("usage") or {})) else None,
                latency_ms=_latency_ms)              # §7.6②：fail-loud（record_event required）

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                if not cfg.is_sub_agent:
                    cfg.sink.cost(*cfg.token_totals())
                break

            cfg.bump_turn()
            budget = cfg.check_budget()
            if budget["exceeded"]:
                cfg.sink.info(f"Budget exceeded: {budget['reason']}")
                emit(BudgetExceeded(reason=budget["reason"]))
                break

            # Phase 1: Parse & permission-check (serial)
            oai_checked: list[dict] = []
            for tc in tool_calls:
                if cfg.is_aborted():
                    break
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                try:
                    inp = json.loads(tc["function"]["arguments"])
                except Exception:
                    inp = {}

                emit(ToolCallRequested(tool=fn_name, input=inp, tool_use_id=tc["id"]))

                allowed, denial = await cfg.authorize(fn_name, inp)
                if not allowed:
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": denial})
                    continue
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            # Phase 2: Group & execute (parallel for consecutive safe tools)
            oai_batches = group_openai_batches(oai_checked)

            oai_context_break = False
            for batch in oai_batches:
                if oai_context_break or cfg.is_aborted():
                    break

                if batch["concurrent"]:
                    async def _run_oai_safe(ct_item: dict) -> tuple[dict, str, int]:
                        _t0 = time.time()
                        raw = await cfg.execute_tool(ct_item["fn"], ct_item["inp"])
                        res = cfg.persist_large_result(ct_item["fn"], raw)
                        _lat = int((time.time() - _t0) * 1000)
                        emit(ToolResultObserved(tool=ct_item["fn"], tool_use_id=ct_item["tc"]["id"],
                                                chars=len(res), result=res))
                        return ct_item, res, _lat

                    results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                    for ct_item, res, _lat in results:
                        tmsg = {"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res}
                        cfg.record_provider_messages(tmsg, latency_ms=_lat)       # §7.6①
                else:
                    for ct in batch["items"]:
                        if not ct["allowed"]:
                            dmsg = {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]}
                            cfg.record_provider_messages(dmsg)   # §7.6①：denied 也是 toolResult,必须落树
                            continue
                        _t_tool = time.time()
                        raw = await cfg.execute_tool(ct["fn"], ct["inp"])
                        res = cfg.persist_large_result(ct["fn"], raw)
                        _lat = int((time.time() - _t_tool) * 1000)
                        emit(ToolResultObserved(tool=ct["fn"], tool_use_id=ct["tc"]["id"],
                                                chars=len(res), result=res))

                        if cfg.consume_context_break():
                            cfg.record_provider_messages({"role": "user", "content": res})
                            oai_context_break = True
                            break
                        rmsg = {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res}
                        cfg.record_provider_messages(rmsg, latency_ms=_lat)       # §7.6①
            if not oai_context_break:
                cfg.inject_skill_bodies()
        return messages

    # ─── Summary compaction（docs/16 #3a/#10）─────────────────────────────────
    #
    # host-driven（经 Agent._compact_* 委托调用，per-instance monkeypatch 锚）：summarizer 是
    # 一次独立 LLM 调用、不在纯 loop 内；真正的 shrink 是 AgentSession.compact 写的 COMPACTION entry。
    # docs/16 #10：summarizer 只吃 **prefix 投影**（kept-suffix 由 AgentSession 的 keepRecentTokens
    # cut-point 排除）——summary 与 fold 保留区绝不双计同一段内容。

    _SUMMARIZE_PROMPT = ("Summarize the conversation so far in a concise paragraph, "
                         "preserving key decisions, file paths, and context needed to continue the work.")

    @staticmethod
    def _with_summary_request(messages: list, prompt_text: str) -> list:
        """把 summarize 指令接到 prefix 末尾。prefix 末条若是 user（render 保证列表内交替，
        但 prefix 截断可停在 user 上），把指令并入该条（anthropic 不接受连续 user）。"""
        msgs = list(messages)
        if msgs and msgs[-1].get("role") == "user":
            last = dict(msgs[-1])
            c = last.get("content")
            if isinstance(c, list):
                last["content"] = list(c) + [{"type": "text", "text": "\n\n" + prompt_text}]
            else:
                last["content"] = (c or "") + "\n\n" + prompt_text
            msgs[-1] = last
        else:
            msgs.append({"role": "user", "content": prompt_text})
        return msgs

    async def _compact_anthropic(self, host, messages: "list | None") -> "str | None":
        if messages is None or len(messages) < 3:
            return None
        summary_resp = await host._anthropic_client.messages.create(
            model=host.model,
            max_tokens=2048,
            system="You are a conversation summarizer. Be concise but preserve important details.",
            messages=self._with_summary_request(messages, self._SUMMARIZE_PROMPT),
        )
        summary_text = summary_resp.content[0].text if summary_resp.content and summary_resp.content[0].type == "text" else "No summary available."
        host.last_input_token_count = 0
        return summary_text

    async def _compact_openai(self, host, messages: "list | None") -> "str | None":
        if messages is None or len(messages) < 4:     # 含 render 注入的 system[0]
            return None
        summary_resp = await host._openai_client.chat.completions.create(
            model=host.model,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *self._with_summary_request(messages[1:], self._SUMMARIZE_PROMPT),
            ],
        )
        summary_text = summary_resp.choices[0].message.content or "No summary available."
        host.last_input_token_count = 0
        return summary_text

    @staticmethod
    def block_to_dict(block) -> dict:
        """Anthropic content block → plain dict for storage（移植自 _block_to_dict）。"""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name,
                    "input": dict(block.input) if hasattr(block.input, "items") else block.input}
        return {"type": block.type}
