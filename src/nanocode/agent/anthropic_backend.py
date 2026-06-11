"""Anthropic 后端：流式对话循环、流式工具早期执行、内容块转字典、摘要压缩（summary-compaction）。

注（docs/14 P5）：原 snip/microcompact 多层 in-place 裁剪 tier（CompressionPipeline）已删除——大工具
输出由 tools.shared 的 per-result cap（MAX_RESULT_CHARS）+ compaction.persist_large_result 控制，
上下文压缩只保留 summary-compaction 一条路径（写 compaction 树 entry）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..tools import get_active_tool_definitions, CONCURRENCY_SAFE_TOOLS
from ..memory import start_memory_prefetch, format_memories_for_injection, MemoryPrefetch
from .models import _get_max_output_tokens, _with_retry


class AnthropicBackendMixin:
    async def _compact_anthropic(self) -> "str | None":
        # Invariant: caller must ensure the last message is a plain user-text
        # message (not a tool_result). We slice it off below; if it were a
        # tool_result, the preceding assistant's tool_use would be orphaned
        # and the API would reject the summarize call.
        if len(self._anthropic_messages) < 4:
            return None
        last_user_msg = self._anthropic_messages[-1]
        summary_resp = await self._anthropic_client.messages.create(
            model=self.model,
            max_tokens=2048,
            system="You are a conversation summarizer. Be concise but preserve important details.",
            messages=[
                *self._anthropic_messages[:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary_text = summary_resp.content[0].text if summary_resp.content and summary_resp.content[0].type == "text" else "No summary available."
        self._anthropic_messages = [
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            self._anthropic_messages.append(last_user_msg)
        self.last_input_token_count = 0
        return summary_text  # S4: engine 据此 additive 写 compaction 树 entry

    # ─── Anthropic backend ───────────────────────────────────────

    async def _chat_anthropic(self, user_message: str) -> None:
        self._anthropic_messages.append({"role": "user", "content": user_message})
        self._tree_record({"role": "user", "content": user_message}, required=True)  # S1: message-end → tree（必写）
        # Auto-compact at turn boundary only — the last message is now plain
        # user text, so the slice in _compact_anthropic won't sever a
        # tool_use ↔ tool_result pair from the previous turn's tool execution.
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

            # Consume memory prefetch if settled (non-blocking poll, zero-wait).
            # Append to last user message to maintain user/assistant alternation.
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        # docs/14 §4.5：写 custom_message tree entry（memory prefetch 已 guard not
                        # is_sub_agent → 主 agent）。树写失败 → flat 兜底，绝不静默丢（P3 review #7）。
                        if not self._tree_custom_message("memory", injection_text):
                            last = self._anthropic_messages[-1] if self._anthropic_messages else None
                            if last and last.get("role") == "user":
                                content = last.get("content", "")
                                if isinstance(content, str):
                                    last["content"] = content + "\n\n" + injection_text
                                elif isinstance(content, list):
                                    content.append({"type": "text", "text": injection_text})
                            else:
                                self._anthropic_messages.append({"role": "user", "content": injection_text})
                        for m in memories:
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass  # prefetch errors already logged

            self._inject_finished_tasks(self._anthropic_messages)
            self._inject_skill_listing(self._anthropic_messages)

            if not self.is_sub_agent:
                self._sink.spinner_start()

            # ── Streaming tool execution ──────────────────────────────
            # As each tool_use content block completes during streaming, check
            # if it's concurrency-safe and auto-allowed. If so, start execution
            # immediately — the tool runs while the model still generates.
            early_executions: dict[str, asyncio.Task] = {}

            def _on_tool_block(block: dict):
                if block["name"] in CONCURRENCY_SAFE_TOOLS:
                    # 早执行判定经同一 PermissionEngine；allowlist 兜底仍在 _execute_tool_call。
                    if self.permission.check(block["name"], block["input"]).action == "allow":
                        task = asyncio.create_task(self._execute_tool_call(block["name"], block["input"]))
                        early_executions[block["id"]] = task

            # S2（docs/13）：从 canonical 树渲染本轮请求（含 S1 消息 + P5 注入 custom_message），
            # 覆盖扁平列表——树是会话事实源，扁平列表降为本轮投影。
            self._anthropic_messages = self._build_request_messages()
            self.tracer.emit(
                "llm_request", model=self.model,
                message_count=len(self._anthropic_messages),
                messages=self._anthropic_messages,
            )
            response = await self._call_anthropic_stream(on_tool_block_complete=_on_tool_block)

            if not self.is_sub_agent:
                self._sink.spinner_stop()

            self.last_api_call_time = time.time()
            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens
            self.last_input_token_count = response.usage.input_tokens

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            assistant_msg = {
                "role": "assistant",
                "content": [self._block_to_dict(b) for b in response.content],
            }
            self._anthropic_messages.append(assistant_msg)
            self._tree_record(assistant_msg, stop_reason=getattr(response, "stop_reason", None))  # S1 + 真实 stopReason
            self._dispatch_event(
                "assistant_message",
                text="".join(b.text for b in response.content if b.type == "text"),
                thinking=getattr(response, "_nanocode_thinking", ""),
                tool_uses=[
                    {"id": b.id, "name": b.name,
                     "input": dict(b.input) if hasattr(b.input, "items") else b.input}
                    for b in response.content if b.type == "tool_use"
                ],
            )
            self.tracer.emit(
                "llm_response",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

            if not tool_uses:
                if not self.is_sub_agent:
                    self._sink.cost(self.total_input_tokens, self.total_output_tokens)
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                self._sink.info(f"Budget exceeded: {budget['reason']}")
                self.tracer.emit("budget_exceeded", reason=budget["reason"])
                break

            # Process tools: early-started ones (from streaming) just await
            # their result; others go through permission check + execution.
            tool_results: list[dict] = []
            context_break = False
            for tu in tool_uses:
                if context_break or self._aborted:
                    break
                inp = dict(tu.input) if hasattr(tu.input, 'items') else tu.input
                self._dispatch_event("tool_call", tool=tu.name, input=inp, tool_use_id=tu.id)

                # Was this tool already started during streaming?
                early_task = early_executions.get(tu.id)
                if early_task:
                    raw = await early_task
                    res = self._persist_large_result(tu.name, raw)
                    self._dispatch_event("tool_result", tool=tu.name, tool_use_id=tu.id, chars=len(res), result=res)
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})
                    continue

                # Permission check for tools not started early（单一决策入口 + 审批）
                allowed, denial = await self._authorize_dispatch(tu.name, inp)
                if not allowed:
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": denial})
                    continue

                raw = await self._execute_tool_call(tu.name, inp)
                res = self._persist_large_result(tu.name, raw)
                self._dispatch_event("tool_result", tool=tu.name, tool_use_id=tu.id, chars=len(res), result=res)

                if self._context_cleared:
                    self._context_cleared = False
                    cb_msg = {"role": "user", "content": res}
                    self._anthropic_messages.append(cb_msg)
                    self._tree_record(cb_msg)  # S1: message-end → tree
                    context_break = True
                    break
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})

            if not context_break and tool_results:
                tr_msg = {"role": "user", "content": tool_results}
                self._anthropic_messages.append(tr_msg)
                self._tree_record(tr_msg)  # S1: message-end → tree
            self._context_cleared = False
            if not context_break:
                self._inject_pending_skill_bodies(self._anthropic_messages)

    @staticmethod
    def _block_to_dict(block) -> dict:
        """Convert an Anthropic content block to a plain dict for storage."""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input) if hasattr(block.input, 'items') else block.input}
        # Fallback
        return {"type": block.type}

    async def _call_anthropic_stream(self, on_tool_block_complete=None):
        """Stream an Anthropic API call. When a tool_use content block finishes
        during streaming, on_tool_block_complete fires immediately so the caller
        can start execution before the full response arrives (streaming tool
        execution triggered on each content block stop)."""
        async def _do():
            max_output = _get_max_output_tokens(self.model)
            create_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_output if self._thinking_mode != "disabled" else 16384,
                "system": self._system_prompt,
                "tools": get_active_tool_definitions(self.tools),
                "messages": self._anthropic_messages,
            }

            if self._thinking_mode in ("adaptive", "enabled"):
                create_params["thinking"] = {"type": "enabled", "budget_tokens": max_output - 1}

            thinking_parts: list[str] = []
            text_blocks: dict[int, list] = {}
            thinking_blocks: dict[int, list] = {}
            # Track in-flight tool_use blocks by index for streaming execution
            tool_blocks_by_index: dict[int, dict] = {}

            async with self._anthropic_client.messages.stream(**create_params) as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue

                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            tool_blocks_by_index[event.index] = {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, 'text'):
                            text_blocks.setdefault(event.index, []).append(delta.text)
                        elif hasattr(delta, 'thinking'):
                            thinking_parts.append(delta.thinking)
                            thinking_blocks.setdefault(event.index, []).append(delta.thinking)
                        elif hasattr(delta, 'partial_json'):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += delta.partial_json

                    elif event.type == "content_block_stop":
                        if event.index in text_blocks:
                            self._sink.spinner_stop()
                            self._emit_block("".join(text_blocks.pop(event.index)))
                        elif event.index in thinking_blocks:
                            buf = thinking_blocks.pop(event.index)
                            # sink 自行决定渲染/抑制（BufferSink 下 thinking/spinner 均 no-op，
                            # 等价于旧 is_sub_agent 抑制）；core 不再判 _output_buffer。
                            self._sink.spinner_stop()
                            self._dispatch_event("assistant_thinking", text="".join(buf))
                        else:
                            tb = tool_blocks_by_index.pop(event.index, None)
                            if tb and on_tool_block_complete:
                                import json as _json
                                try:
                                    parsed = _json.loads(tb["input_json"] or "{}")
                                except Exception:
                                    parsed = {}
                                on_tool_block_complete({
                                    "type": "tool_use", "id": tb["id"],
                                    "name": tb["name"], "input": parsed,
                                })

                final_message = await stream.get_final_message()

            # Filter out thinking blocks
            final_message._nanocode_thinking = "".join(thinking_parts)
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message

        return await _with_retry(_do, on_retry=self._sink.retry)
