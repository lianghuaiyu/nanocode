"""OpenAI 兼容后端：流式对话循环、并行/串行工具批处理、流式响应组装、摘要压缩（summary-compaction）。

注（docs/14 P5）：原 snip/microcompact 分层裁剪 tier 已删除——大工具输出由 tools.shared 的
per-result cap（MAX_RESULT_CHARS）控制，上下文压缩只保留 summary-compaction 一条路径。"""

from __future__ import annotations

import asyncio
import json
import time

from ..tools import get_active_tool_definitions, CONCURRENCY_SAFE_TOOLS
from ..memory import start_memory_prefetch, format_memories_for_injection, MemoryPrefetch
from .models import _to_openai_tools, _with_retry


class OpenAIBackendMixin:
    async def _compact_openai(self) -> "str | None":
        # Invariant: caller must ensure the last message is a plain user-text
        # message (not a `tool` role result). Same reasoning as
        # _compact_anthropic — slicing off a tool result would orphan the
        # preceding assistant's tool_calls.
        if len(self._openai_messages) < 5:
            return None
        system_msg = self._openai_messages[0]
        last_user_msg = self._openai_messages[-1]
        summary_resp = await self._openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *self._openai_messages[1:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.choices[0].message.content or "No summary available."
        self._openai_messages = [
            system_msg,
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            self._openai_messages.append(last_user_msg)
        self.last_input_token_count = 0
        return summary_text  # S4: engine 据此 additive 写 compaction 树 entry

    # ─── OpenAI-compatible backend ───────────────────────────────

    async def _chat_openai(self, user_message: str) -> None:
        self._openai_messages.append({"role": "user", "content": user_message})
        self._tree_record({"role": "user", "content": user_message})  # S1: message-end → tree
        # Auto-compact at turn boundary only — see _chat_anthropic for rationale.
        # The last message is now plain user text, so the slice in
        # _compact_openai won't orphan a tool_calls / tool message pair.
        await self._check_and_compact()

        # Start async memory prefetch (non-blocking, fires once per user turn)
        memory_prefetch: MemoryPrefetch | None = None
        if not self.is_sub_agent:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                    backend=self._memory_backend,
                )

        while True:
            if self._aborted:
                break

            # Consume memory prefetch if settled (non-blocking poll, zero-wait)
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        # docs/14 §4.5：写 custom_message tree entry（主 agent）；树写失败 → flat 兜底（P3 review #7）。
                        if not self._tree_custom_message("memory", injection_text):
                            last = self._openai_messages[-1] if self._openai_messages else None
                            if last and last.get("role") == "user":
                                last["content"] = (last.get("content") or "") + "\n\n" + injection_text
                            else:
                                self._openai_messages.append({"role": "user", "content": injection_text})
                        for m in memories:
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass  # prefetch errors already logged

            self._inject_finished_tasks(self._openai_messages)
            self._inject_skill_listing(self._openai_messages)

            if not self.is_sub_agent:
                self._sink.spinner_start()

            # S2（docs/13）：从 canonical 树渲染本轮请求（含 S1 消息 + P5 注入 custom_message），
            # 覆盖扁平列表——树是会话事实源，扁平列表降为本轮投影。
            self._openai_messages = self._build_request_messages()
            self.tracer.emit(
                "llm_request", model=self.model,
                message_count=len(self._openai_messages),
                messages=self._openai_messages,
            )
            response = await self._call_openai_stream()

            if not self.is_sub_agent:
                self._sink.spinner_stop()

            self.last_api_call_time = time.time()

            if response.get("usage"):
                self.total_input_tokens += response["usage"]["prompt_tokens"]
                self.total_output_tokens += response["usage"]["completion_tokens"]
                self.last_input_token_count = response["usage"]["prompt_tokens"]

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            message = choice.get("message", {})

            self._openai_messages.append(message)
            self._tree_record(message, stop_reason=choice.get("finish_reason"))  # S1 + 真实 finish_reason

            _tcs = message.get("tool_calls") or []
            self._dispatch_event(
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
            _usage = response.get("usage") or {}
            self.tracer.emit(
                "llm_response",
                input_tokens=_usage.get("prompt_tokens", 0),
                output_tokens=_usage.get("completion_tokens", 0),
            )

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                if not self.is_sub_agent:
                    self._sink.cost(self.total_input_tokens, self.total_output_tokens)
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                self._sink.info(f"Budget exceeded: {budget['reason']}")
                self.tracer.emit("budget_exceeded", reason=budget["reason"])
                break

            # Phase 1: Parse & permission-check (serial)
            oai_checked: list[dict] = []
            for tc in tool_calls:
                if self._aborted:
                    break
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                try:
                    inp = json.loads(tc["function"]["arguments"])
                except Exception:
                    inp = {}

                self._dispatch_event("tool_call", tool=fn_name, input=inp, tool_use_id=tc["id"])

                # 单一决策入口（policy + 审批）；allowlist 兜底在 _execute_tool_call。
                allowed, denial = await self._authorize_dispatch(fn_name, inp)
                if not allowed:
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": denial})
                    continue
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            # Phase 2: Group & execute (parallel for consecutive safe tools)
            oai_batches: list[dict] = []
            for ct in oai_checked:
                safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
                if safe and oai_batches and oai_batches[-1]["concurrent"]:
                    oai_batches[-1]["items"].append(ct)
                else:
                    oai_batches.append({"concurrent": safe, "items": [ct]})

            oai_context_break = False
            for batch in oai_batches:
                if oai_context_break or self._aborted:
                    break

                if batch["concurrent"]:
                    async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
                        raw = await self._execute_tool_call(ct_item["fn"], ct_item["inp"])
                        res = self._persist_large_result(ct_item["fn"], raw)
                        self._dispatch_event("tool_result", tool=ct_item["fn"], tool_use_id=ct_item["tc"]["id"], chars=len(res), result=res)
                        return ct_item, res

                    results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                    for ct_item, res in results:
                        tmsg = {"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res}
                        self._openai_messages.append(tmsg)
                        self._tree_record(tmsg)  # S1: message-end → tree
                else:
                    for ct in batch["items"]:
                        if not ct["allowed"]:
                            dmsg = {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]}
                            self._openai_messages.append(dmsg)
                            self._tree_record(dmsg)  # S1: message-end → tree
                            continue
                        raw = await self._execute_tool_call(ct["fn"], ct["inp"])
                        res = self._persist_large_result(ct["fn"], raw)
                        self._dispatch_event("tool_result", tool=ct["fn"], tool_use_id=ct["tc"]["id"], chars=len(res), result=res)

                        if self._context_cleared:
                            self._context_cleared = False
                            cbmsg = {"role": "user", "content": res}
                            self._openai_messages.append(cbmsg)
                            self._tree_record(cbmsg)  # S1: message-end → tree
                            oai_context_break = True
                            break
                        rmsg = {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res}
                        self._openai_messages.append(rmsg)
                        self._tree_record(rmsg)  # S1: message-end → tree

            self._context_cleared = False
            if not oai_context_break:
                self._inject_pending_skill_bodies(self._openai_messages)

    async def _call_openai_stream(self) -> dict:
        async def _do():
            stream = await self._openai_client.chat.completions.create(
                model=self.model,
                tools=_to_openai_tools(get_active_tool_definitions(self.tools)),
                messages=self._openai_messages,
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta and delta.content:
                    content += delta.content

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index)
                        if existing:
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += tc.function.arguments
                        else:
                            tool_calls[tc.index] = {
                                "id": tc.id or "",
                                "name": (tc.function.name if tc.function else "") or "",
                                "arguments": (tc.function.arguments if tc.function else "") or "",
                            }

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            if content:
                self._sink.spinner_stop()
                self._emit_block(content)

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do, on_retry=self._sink.retry)
