"""agent/providers.py — ProviderAdapter：把 provider-specific 的流式 + capture + 请求组装收敛到
一个接口背后（docs/15 §5/§13#1）。

STEP B（providers seam）：先**包裹**（不删）现有 `_call_anthropic_stream` / `_call_openai_stream`
的 SDK 流式机制,使两后端 mixin 委托同一接口。这冻结两条流式行为、为 STEP C 把循环上移到
`agent/core.py` 做准备。capture（provider msg → 中立 Message）与 neutral_stop_reason 也归口于此
（§5：adapter 持有 build_request / stream / capture / cache 控制）。

不变量：adapter 无状态地持有 client；不写 session、不碰 AgentState 的 durable 事实——只做 SDK I/O
+ provider 整形/归一。UI 副作用经 StreamCallbacks 注入（spinner/text/thinking/tool-block/retry）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..session import capture
from ..tools import get_active_tool_definitions
from .models import _get_max_output_tokens, _to_openai_tools, _with_retry


def _noop(*_a, **_k) -> None:  # 默认 callback：无表现层时全 no-op
    return None


@dataclass
class StreamCallbacks:
    """流式期间的 UI 副作用注入点（fire-and-forget，绝不影响控制流）。

    与旧 `_call_*_stream` 内联的 UI 副作用一一对应（docs/16 #2 后由 host.emit 的 typed 事件承载）：
    - spinner_stop：首个 text/thinking block 收尾前停 spinner；
    - text_block：完整 text block（UI markdown，= host._emit_block → AssistantDelta(text)）；
    - thinking_block：完整 thinking block（= emit AssistantDelta(thinking)）；
    - tool_block：tool_use block 收尾（流式早执行触发，= on_tool_block_complete）；
    - retry：重试通知（= self._sink.retry），None 则不通知。
    """

    spinner_stop: Callable[[], None] = _noop
    text_block: Callable[[str], None] = _noop
    thinking_block: Callable[[str], None] = _noop
    tool_block: Callable[[dict], None] = _noop
    retry: "Callable[[int, int, str], None] | None" = None


class ProviderAdapter:
    """provider 适配器基类。持 client；stream() 做 SDK 流式;capture/neutral_stop_reason 归一。"""

    name: str = ""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def stream(self, *, model: str, system: "str | None", tools: list,
                     messages: list, thinking_mode: str, callbacks: StreamCallbacks) -> Any:
        raise NotImplementedError

    def neutral_stop_reason(self, raw: "str | None") -> "str | None":
        return capture.neutral_stop_reason(self.name, raw)

    def capture(self, msg: dict, *, model: str, stop_reason: "str | None" = None,
                usage: "dict | None" = None, latency_ms: "int | None" = None) -> list[dict]:
        """provider 消息 → 中立 Message[]（§5：capture 归 adapter）。stop_reason 须已是中立值。"""
        cap = capture.capture_openai if self.name == "openai" else capture.capture_anthropic
        return cap(msg, model=model, stop_reason=stop_reason, usage=usage, latency_ms=latency_ms)


class AnthropicAdapter(ProviderAdapter):
    """Anthropic 流式（移植自 engine.AnthropicBackendMixin._call_anthropic_stream，行为逐字一致）。"""

    name = "anthropic"

    async def stream(self, *, model, system, tools, messages, thinking_mode, callbacks):
        async def _do():
            max_output = _get_max_output_tokens(model)
            create_params: dict[str, Any] = {
                "model": model,
                "max_tokens": max_output if thinking_mode != "disabled" else 16384,
                "system": system,
                "tools": get_active_tool_definitions(tools),
                "messages": messages,
            }
            if thinking_mode in ("adaptive", "enabled"):
                create_params["thinking"] = {"type": "enabled", "budget_tokens": max_output - 1}

            thinking_parts: list[str] = []
            text_blocks: dict[int, list] = {}
            thinking_blocks: dict[int, list] = {}
            tool_blocks_by_index: dict[int, dict] = {}

            async with self.client.messages.stream(**create_params) as stream:
                async for event in stream:
                    if not hasattr(event, "type"):
                        continue
                    if event.type == "content_block_start":
                        cb = getattr(event, "content_block", None)
                        if cb and getattr(cb, "type", None) == "tool_use":
                            tool_blocks_by_index[event.index] = {"id": cb.id, "name": cb.name, "input_json": ""}
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            text_blocks.setdefault(event.index, []).append(delta.text)
                        elif hasattr(delta, "thinking"):
                            thinking_parts.append(delta.thinking)
                            thinking_blocks.setdefault(event.index, []).append(delta.thinking)
                        elif hasattr(delta, "partial_json"):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += delta.partial_json
                    elif event.type == "content_block_stop":
                        if event.index in text_blocks:
                            callbacks.spinner_stop()
                            callbacks.text_block("".join(text_blocks.pop(event.index)))
                        elif event.index in thinking_blocks:
                            buf = thinking_blocks.pop(event.index)
                            callbacks.spinner_stop()
                            callbacks.thinking_block("".join(buf))
                        else:
                            tb = tool_blocks_by_index.pop(event.index, None)
                            if tb:
                                try:
                                    parsed = json.loads(tb["input_json"] or "{}")
                                except Exception:
                                    parsed = {}
                                callbacks.tool_block({"type": "tool_use", "id": tb["id"],
                                                      "name": tb["name"], "input": parsed})

                final_message = await stream.get_final_message()

            final_message._nanocode_thinking = "".join(thinking_parts)
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message

        return await _with_retry(_do, on_retry=callbacks.retry)


class OpenAIAdapter(ProviderAdapter):
    """OpenAI 兼容流式（移植自 engine.OpenAIBackendMixin._call_openai_stream，行为逐字一致）。
    system 在 messages[0]、不走 thinking——签名保持与 AnthropicAdapter 一致,忽略 system/thinking_mode。"""

    name = "openai"

    async def stream(self, *, model, system, tools, messages, thinking_mode, callbacks):
        async def _do():
            stream = await self.client.chat.completions.create(
                model=model,
                tools=_to_openai_tools(get_active_tool_definitions(tools)),
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            async for chunk in stream:
                if chunk.usage:
                    usage = {"prompt_tokens": chunk.usage.prompt_tokens,
                             "completion_tokens": chunk.usage.completion_tokens}
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
                callbacks.spinner_stop()
                callbacks.text_block(content)

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {"role": "assistant", "content": content or None, "tool_calls": assembled},
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do, on_retry=callbacks.retry)


def make_provider_adapter(*, use_openai: bool, anthropic_client, openai_client) -> ProviderAdapter:
    """按 use_openai 选 adapter（engine.__init__ 与子 agent 构造共用）。"""
    return OpenAIAdapter(openai_client) if use_openai else AnthropicAdapter(anthropic_client)
