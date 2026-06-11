"""agent/core.py — AgentCore：模型循环 + 流式消费 + tool 调度 + 事件发射（docs/15 §6）。

STEP C：把原 AnthropicBackendMixin._chat_anthropic / OpenAIBackendMixin._chat_openai 的循环体
上移到这里,driven by 注入的 `host`（当前 = Agent，提供 collaborators）。逐字搬迁、行为不变,
full suite 持续 exercise（等价性证明）。

§6 边界：AgentCore 自身**不**直接写 session、不 build system prompt、不发现 skills/subagents/MCP、
不写 artifacts、不持 durable provider messages —— 这些都经 host 方法委托（host._tree_record /
host._build_request_messages / host._execute_tool_call / host._inject_* …）。后续 STEP D/E/F 把
host 委托逐个替换为 AgentSession.record_event / ContextRuntime / CapabilityRouter,host 角色随之收缩。

provider-specific 的两条循环变体保留（adapter-driven）；不强行合一。
"""

from __future__ import annotations

import asyncio
import json
import time

from ..memory import start_memory_prefetch, format_memories_for_injection
from ..session import tree as _tree
from ..tools import CONCURRENCY_SAFE_TOOLS
from .loop import group_openai_batches
from .providers import StreamCallbacks


class AgentCore:
    """模型循环宿主。无状态（循环态都是 turn-local 局部变量或 host 上的字段）；按 host.use_openai 分派。"""

    async def run_turn(self, host, user_message: str) -> None:
        if host.use_openai:
            await self.run_openai_turn(host, user_message)
        else:
            await self.run_anthropic_turn(host, user_message)

    # ─── Anthropic turn（移植自 _chat_anthropic，self→host）────────────────────
    async def run_anthropic_turn(self, host, user_message: str) -> None:
        host._anthropic_messages.append({"role": "user", "content": user_message})
        host._tree_record({"role": "user", "content": user_message}, required=True)  # S1: message-end → tree（必写）
        await host._check_and_compact()

        memory_prefetch = None
        if not host.is_sub_agent:
            sq = host._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    host._already_surfaced_memories, host._session_memory_bytes,
                    backend=host._memory_backend,
                )

        while True:
            if host._aborted:
                break

            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        if not host._tree_custom_message("memory", injection_text):
                            last = host._anthropic_messages[-1] if host._anthropic_messages else None
                            if last and last.get("role") == "user":
                                content = last.get("content", "")
                                if isinstance(content, str):
                                    last["content"] = content + "\n\n" + injection_text
                                elif isinstance(content, list):
                                    content.append({"type": "text", "text": injection_text})
                            else:
                                host._anthropic_messages.append({"role": "user", "content": injection_text})
                        for m in memories:
                            host._already_surfaced_memories.add(m.path)
                            host._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass

            host._inject_finished_tasks(host._anthropic_messages)
            host._inject_skill_listing(host._anthropic_messages)

            if not host.is_sub_agent:
                host._sink.spinner_start()

            early_executions: dict[str, asyncio.Task] = {}
            early_started: dict[str, float] = {}

            def _on_tool_block(block: dict):
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    if host.permission.check(block["name"], block["input"]).action == "allow":
                        early_started[block["id"]] = time.time()
                        task = asyncio.create_task(host._execute_tool_call(block["name"], block["input"]))
                        early_executions[block["id"]] = task

            host._anthropic_messages = host._build_request_messages()
            host._tree_event(_tree.LLM_REQUEST, model=host.model,
                             messageCount=len(host._anthropic_messages),
                             messagesChars=len(json.dumps(host._anthropic_messages, default=str)))
            _t_req = time.time()
            cb = StreamCallbacks(
                spinner_stop=host._sink.spinner_stop,
                text_block=host._emit_block,
                thinking_block=lambda t: host._dispatch_event("assistant_thinking", text=t),
                tool_block=_on_tool_block,
                retry=host._sink.retry,
            )
            response = await host._provider.stream(
                model=host.model, system=host._system_prompt, tools=host.tools,
                messages=host._anthropic_messages, thinking_mode=host._thinking_mode, callbacks=cb)
            _latency_ms = int((time.time() - _t_req) * 1000)

            if not host.is_sub_agent:
                host._sink.spinner_stop()

            host.last_api_call_time = time.time()
            host.total_input_tokens += response.usage.input_tokens
            host.total_output_tokens += response.usage.output_tokens
            host.last_input_token_count = response.usage.input_tokens

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            assistant_msg = {
                "role": "assistant",
                "content": [self.block_to_dict(b) for b in response.content],
            }
            host._anthropic_messages.append(assistant_msg)
            host._tree_record(assistant_msg, stop_reason=getattr(response, "stop_reason", None),
                              usage={"inputTokens": response.usage.input_tokens,
                                     "outputTokens": response.usage.output_tokens},
                              latency_ms=_latency_ms)
            host._dispatch_event(
                "assistant_message",
                text="".join(b.text for b in response.content if b.type == "text"),
                thinking=getattr(response, "_nanocode_thinking", ""),
                tool_uses=[
                    {"id": b.id, "name": b.name,
                     "input": dict(b.input) if hasattr(b.input, "items") else b.input}
                    for b in response.content if b.type == "tool_use"
                ],
            )

            if not tool_uses:
                if not host.is_sub_agent:
                    host._sink.cost(host.total_input_tokens, host.total_output_tokens)
                break

            host.current_turns += 1
            budget = host._check_budget()
            if budget["exceeded"]:
                host._sink.info(f"Budget exceeded: {budget['reason']}")
                host._tree_event(_tree.BUDGET_EXCEEDED, reason=budget["reason"])
                break

            tool_results: list[dict] = []
            context_break = False
            for tu in tool_uses:
                if context_break or host._aborted:
                    break
                inp = dict(tu.input) if hasattr(tu.input, "items") else tu.input
                host._dispatch_event("tool_call", tool=tu.name, input=inp, tool_use_id=tu.id)

                early_task = early_executions.get(tu.id)
                if early_task:
                    raw = await early_task
                    res = host._persist_large_result(tu.name, raw)
                    _lat = int((time.time() - early_started.get(tu.id, time.time())) * 1000)
                    host._dispatch_event("tool_result", tool=tu.name, tool_use_id=tu.id, chars=len(res), result=res)
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res,
                                         "toolName": tu.name, "toolLatencyMs": _lat})
                    continue

                allowed, denial = await host._authorize_dispatch(tu.name, inp)
                if not allowed:
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": denial,
                                         "toolName": tu.name, "is_error": True})
                    continue

                _t_tool = time.time()
                raw = await host._execute_tool_call(tu.name, inp)
                res = host._persist_large_result(tu.name, raw)
                _lat = int((time.time() - _t_tool) * 1000)
                host._dispatch_event("tool_result", tool=tu.name, tool_use_id=tu.id, chars=len(res), result=res)

                if host._context_cleared:
                    host._context_cleared = False
                    cb_msg = {"role": "user", "content": res}
                    host._anthropic_messages.append(cb_msg)
                    host._tree_record(cb_msg)
                    context_break = True
                    break
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res,
                                     "toolName": tu.name, "toolLatencyMs": _lat})

            if not context_break and tool_results:
                tr_msg = {"role": "user", "content": tool_results}
                host._anthropic_messages.append(tr_msg)
                host._tree_record(tr_msg)
            host._context_cleared = False
            if not context_break:
                host._inject_pending_skill_bodies(host._anthropic_messages)

    # ─── OpenAI turn（移植自 _chat_openai，self→host）──────────────────────────
    async def run_openai_turn(self, host, user_message: str) -> None:
        host._openai_messages.append({"role": "user", "content": user_message})
        host._tree_record({"role": "user", "content": user_message}, required=True)  # S1: message-end → tree（必写）
        await host._check_and_compact()

        memory_prefetch = None
        if not host.is_sub_agent:
            sq = host._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    host._already_surfaced_memories, host._session_memory_bytes,
                    backend=host._memory_backend,
                )

        while True:
            if host._aborted:
                break

            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        if not host._tree_custom_message("memory", injection_text):
                            last = host._openai_messages[-1] if host._openai_messages else None
                            if last and last.get("role") == "user":
                                last["content"] = (last.get("content") or "") + "\n\n" + injection_text
                            else:
                                host._openai_messages.append({"role": "user", "content": injection_text})
                        for m in memories:
                            host._already_surfaced_memories.add(m.path)
                            host._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass

            host._inject_finished_tasks(host._openai_messages)
            host._inject_skill_listing(host._openai_messages)

            if not host.is_sub_agent:
                host._sink.spinner_start()

            host._openai_messages = host._build_request_messages()
            host._tree_event(_tree.LLM_REQUEST, model=host.model,
                             messageCount=len(host._openai_messages),
                             messagesChars=len(json.dumps(host._openai_messages, default=str)))
            _t_req = time.time()
            cb = StreamCallbacks(spinner_stop=host._sink.spinner_stop,
                                 text_block=host._emit_block, retry=host._sink.retry)
            response = await host._provider.stream(
                model=host.model, system=None, tools=host.tools,
                messages=host._openai_messages, thinking_mode=host._thinking_mode, callbacks=cb)
            _latency_ms = int((time.time() - _t_req) * 1000)

            if not host.is_sub_agent:
                host._sink.spinner_stop()

            host.last_api_call_time = time.time()

            if response.get("usage"):
                host.total_input_tokens += response["usage"]["prompt_tokens"]
                host.total_output_tokens += response["usage"]["completion_tokens"]
                host.last_input_token_count = response["usage"]["prompt_tokens"]

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            host._openai_messages.append(message)
            host._tree_record(message, stop_reason=choice.get("finish_reason"),
                              usage={"inputTokens": _u.get("prompt_tokens", 0),
                                     "outputTokens": _u.get("completion_tokens", 0)}
                              if (_u := (response.get("usage") or {})) else None,
                              latency_ms=_latency_ms)

            _tcs = message.get("tool_calls") or []
            host._dispatch_event(
                "assistant_message",
                text=message.get("content") or "",
                thinking="",
                tool_uses=[
                    {"id": tc.get("id"),
                     "name": (tc.get("function") or {}).get("name"),
                     "input": (tc.get("function") or {}).get("arguments")}
                    for tc in _tcs
                ],
            )

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                if not host.is_sub_agent:
                    host._sink.cost(host.total_input_tokens, host.total_output_tokens)
                break

            host.current_turns += 1
            budget = host._check_budget()
            if budget["exceeded"]:
                host._sink.info(f"Budget exceeded: {budget['reason']}")
                host._tree_event(_tree.BUDGET_EXCEEDED, reason=budget["reason"])
                break

            # Phase 1: Parse & permission-check (serial)
            oai_checked: list[dict] = []
            for tc in tool_calls:
                if host._aborted:
                    break
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                try:
                    inp = json.loads(tc["function"]["arguments"])
                except Exception:
                    inp = {}

                host._dispatch_event("tool_call", tool=fn_name, input=inp, tool_use_id=tc["id"])

                allowed, denial = await host._authorize_dispatch(fn_name, inp)
                if not allowed:
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": denial})
                    continue
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            # Phase 2: Group & execute (parallel for consecutive safe tools)
            oai_batches = group_openai_batches(oai_checked)

            oai_context_break = False
            for batch in oai_batches:
                if oai_context_break or host._aborted:
                    break

                if batch["concurrent"]:
                    async def _run_oai_safe(ct_item: dict) -> tuple[dict, str, int]:
                        _t0 = time.time()
                        raw = await host._execute_tool_call(ct_item["fn"], ct_item["inp"])
                        res = host._persist_large_result(ct_item["fn"], raw)
                        _lat = int((time.time() - _t0) * 1000)
                        host._dispatch_event("tool_result", tool=ct_item["fn"], tool_use_id=ct_item["tc"]["id"], chars=len(res), result=res)
                        return ct_item, res, _lat

                    results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                    for ct_item, res, _lat in results:
                        tmsg = {"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res}
                        host._openai_messages.append(tmsg)
                        host._tree_record(tmsg, latency_ms=_lat)
                else:
                    for ct in batch["items"]:
                        if not ct["allowed"]:
                            dmsg = {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]}
                            host._openai_messages.append(dmsg)
                            host._tree_record(dmsg)
                            continue
                        _t_tool = time.time()
                        raw = await host._execute_tool_call(ct["fn"], ct["inp"])
                        res = host._persist_large_result(ct["fn"], raw)
                        _lat = int((time.time() - _t_tool) * 1000)
                        host._dispatch_event("tool_result", tool=ct["fn"], tool_use_id=ct["tc"]["id"], chars=len(res), result=res)

                        if host._context_cleared:
                            host._context_cleared = False
                            cbmsg = {"role": "user", "content": res}
                            host._openai_messages.append(cbmsg)
                            host._tree_record(cbmsg)
                            oai_context_break = True
                            break
                        rmsg = {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res}
                        host._openai_messages.append(rmsg)
                        host._tree_record(rmsg, latency_ms=_lat)

            host._context_cleared = False
            if not oai_context_break:
                host._inject_pending_skill_bodies(host._openai_messages)

    # ─── Summary compaction（移植自 mixin，host-driven）────────────────────────
    async def _compact_anthropic(self, host) -> "str | None":
        # Invariant: caller must ensure the last message is a plain user-text message.
        if len(host._anthropic_messages) < 4:
            return None
        last_user_msg = host._anthropic_messages[-1]
        summary_resp = await host._anthropic_client.messages.create(
            model=host.model,
            max_tokens=2048,
            system="You are a conversation summarizer. Be concise but preserve important details.",
            messages=[
                *host._anthropic_messages[:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.content[0].text if summary_resp.content and summary_resp.content[0].type == "text" else "No summary available."
        host._anthropic_messages = [
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            host._anthropic_messages.append(last_user_msg)
        host.last_input_token_count = 0
        return summary_text

    async def _compact_openai(self, host) -> "str | None":
        if len(host._openai_messages) < 5:
            return None
        system_msg = host._openai_messages[0]
        last_user_msg = host._openai_messages[-1]
        summary_resp = await host._openai_client.chat.completions.create(
            model=host.model,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *host._openai_messages[1:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.choices[0].message.content or "No summary available."
        host._openai_messages = [
            system_msg,
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            host._openai_messages.append(last_user_msg)
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
