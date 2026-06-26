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


def is_context_overflow_error(e: BaseException) -> bool:
    """provider 上下文溢出判定（docs/16 #10：overflow 恢复的触发器）。

    保守的字符串匹配（provider SDK 异常类型/版本各异，但溢出文案稳定）：
    anthropic 'prompt is too long' / 'input length and `max_tokens` exceed'；
    openai 'context_length_exceeded'（code）/ 'maximum context length'（message）。
    """
    text = str(e).lower()
    return any(m in text for m in (
        "prompt is too long",
        "input length and `max_tokens` exceed",
        "context_length_exceeded",
        "maximum context length",
    ))


def _noop(*_a, **_k) -> None:  # 默认 callback：无表现层时全 no-op
    return None


@dataclass
class Completion:
    """归一的「流完」终态（B2-a：post-stream 单循环喂养面，docs/16）。

    把两 provider 的完成载荷收敛到同一形状,使 AgentCore 的循环 provider-agnostic：
    - ``assistant_message``：**provider-shaped** assistant 消息 dict——直接喂 record_provider_messages
      （capture 按 adapter.name 选表归一,wire 形状与各自现状逐字一致）；
    - ``tool_calls``：**统一**的 [{id, name, input}]（已解析 input；空列表 = 自然停）；
    - ``usage``：(input_tokens, output_tokens)；**None** = 该完成载荷未携带 usage（OpenAI 无
      ``usage`` 字段时——非合规 provider/mock），core 据此守卫不 add_usage、不落 usage 树键
      （byte-equivalent 于旧 run_openai_turn 的 `if response.get("usage")` 守卫）。Anthropic 恒有 .usage。
    - ``stop_reason``：provider **原生**停止原因（record 时再经 neutral_stop_reason 映射）。

    由 ``ProviderAdapter.complete(raw)`` 从该 provider 的 stream() 原始返回归一产出
    （Anthropic SDK 对象 / OpenAI 合成 dict）——stream() 的流式机制/返回**不变**,新增的
    complete() 只做「原始完成载荷 → Completion」的纯整形,使所有 mock stream 的测试不受影响。
    """

    assistant_message: dict
    tool_calls: list
    usage: "tuple | None"
    stop_reason: "str | None" = None


@dataclass
class StreamCallbacks:
    """流式期间的 UI 副作用注入点（fire-and-forget，绝不影响控制流）。

    与旧 `_call_*_stream` 内联的 UI 副作用一一对应（docs/16 #2 后由 host.emit 的 typed 事件承载）：
    - spinner_stop：默认 no-op（docs/17 Phase 2：spinner 改订阅端从事件流派生，不再经 callback）；
    - text_block：完整 text block（UI markdown，= host._emit_block → AssistantDelta(text)）；
    - thinking_block：完整 thinking block（= emit AssistantDelta(thinking)）；
    - retry：重试通知（docs/17 Phase 2：= emit(RetryRaised)），None 则不通知。
    """

    spinner_stop: Callable[[], None] = _noop
    text_block: Callable[[str], None] = _noop
    thinking_block: Callable[[str], None] = _noop
    retry: "Callable[[int, int, str], None] | None" = None


class ProviderAdapter:
    """provider 适配器基类。持 client；stream() 做 SDK 流式;capture/neutral_stop_reason 归一。

    B1（docs/16 provider seam）：provider-specific 的三件事下沉到 adapter 缝之下，turn-shell/
    压缩/摘要/系统提示词的调用点不再分支 provider：
    - ``capture_api``：capture/render 的 api 串（anthropic / openai-completions）；
    - ``places_system_in_messages``：system 是否进 messages[0]（openai=True，anthropic=False，
      走 out-of-band system kwarg）——render(system_prompt=...) 与 ProviderProjection.system 的
      放置规则真源；
    - ``summarize``：compaction / branch-summary 的一次性 LLM 调用（per-provider override，逐字
      保留各自旧 _compact_*/_summarize_* 的并发体）。
    """

    name: str = ""
    capture_api: str = ""
    places_system_in_messages: bool = False

    def __init__(self, client: Any, *, registry=None) -> None:
        self.client = client
        # docs/24 Phase 4a：per-agent overlay registry——active-tool 过滤读其激活集（tool_search
        # 激活落在 agent registry 上）。None → 全局 REGISTRY（行为等价，旧路径/裸构造）。
        self._registry = registry

    def _active_tools(self, tools: list) -> list:
        return get_active_tool_definitions(tools, registry=self._registry)

    async def stream(self, *, model: str, system: "str | None", tools: list,
                     messages: list, thinking_mode: str, callbacks: StreamCallbacks) -> Any:
        raise NotImplementedError

    def complete(self, raw: Any) -> Completion:
        """stream() 原始完成载荷 → 归一 Completion（B2-a）。per-provider override。"""
        raise NotImplementedError

    def tool_result_messages(self, results: list) -> list:
        """已执行的工具结果 → provider-shaped tool-result 消息 + 其 record latency_ms（B2-a）。

        入参 results：[{tool_call_id, name, content, is_error, latency_ms}, ...]（按调用次序）。
        返回 [(provider_msg: dict, latency_ms: int | None), ...]——循环逐条
        ``record_provider_messages(msg, latency_ms=lat)``。

        两形状（与各自现状逐字一致）：
        - Anthropic：**1 条**批量 user 消息（content=[tool_result...]，per-block toolLatencyMs 内嵌；
          record 不带 latency_ms kwarg → 返回的 latency 恒 None）；
        - OpenAI：**N 条**逐 {"role":"tool",...}（per-call latency 经 record 的 latency_ms kwarg）。

        签名说明：返回 (msg, latency_ms) 对而非裸 dict——OpenAI 的 per-tool 延迟经 record kwarg
        进树（events_from_provider_message 透传 latency_ms），裸 dict 会丢该路径。Anthropic 把延迟
        内嵌在 content block,故 latency 恒 None。这保住两 provider 的 latencyMs 树字段 byte-equivalent。"""
        raise NotImplementedError

    async def summarize(self, *, model: str, messages: "list | None", system_persona: str,
                        instruction: str, max_output_tokens: int, min_messages: int,
                        strip_leading_system: bool = False) -> "str | None":
        """一次性 summarizer 调用（compaction / branch summary）。返回 summary 文本或 None。

        ``messages`` 是 caller 已渲染好的 provider-shaped 前缀/transcript；``instruction`` 接在末尾
        （_with_summary_request）。``min_messages`` 是长度守卫下限（< 则返回 None；含 in-band system 的
        provider 自带 +1）。host.last_input_token_count=0 由调用方写（adapter 不碰 session/AgentState）。

        ``strip_leading_system``（**parity 雷 A**）：caller 渲染前缀时是否已把 system 放进 messages[0]
        ——compaction 前缀经 render(system_prompt=...) 渲染（OpenAI 带 system[0]，须切片避免 double-place）；
        branch-summary 的 user-only transcript 未渲染 system（不切）。Anthropic 走 out-of-band system，
        此标志对其无影响（恒不切）。

        per-provider override 逐字保留旧 _compact_*/_summarize_* 的并发体（in-band vs out-of-band
        system、messages[1:] 切片、长度守卫、max_tokens、解析、fallback 全部 byte-equivalent）。"""
        raise NotImplementedError

    def neutral_stop_reason(self, raw: "str | None") -> "str | None":
        return capture.neutral_stop_reason(self.name, raw)

    def capture(self, msg: dict, *, model: str, stop_reason: "str | None" = None,
                usage: "dict | None" = None, latency_ms: "int | None" = None) -> list[dict]:
        """provider 消息 → 中立 Message[]（§5：capture 归 adapter）。stop_reason 须已是中立值。"""
        cap = capture.capture_openai if self.name == "openai" else capture.capture_anthropic
        return cap(msg, model=model, stop_reason=stop_reason, usage=usage, latency_ms=latency_ms)

    @staticmethod
    def _with_summary_request(messages: list, prompt_text: str) -> list:
        """把 summarize 指令接到前缀末尾（移植自 AgentCore._with_summary_request，逐字一致）。
        prefix 末条若是 user（render 保证交替，但前缀截断可停在 user 上），把指令并入该条
        （anthropic 不接受连续 user）。"""
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


class AnthropicAdapter(ProviderAdapter):
    """Anthropic 流式（移植自 engine.AnthropicBackendMixin._call_anthropic_stream，行为逐字一致）。"""

    name = "anthropic"
    capture_api = "anthropic"
    places_system_in_messages = False

    async def summarize(self, *, model, messages, system_persona, instruction,
                        max_output_tokens, min_messages, strip_leading_system=False):
        # 逐字保留旧 _compact_anthropic / _summarize_anthropic 体（out-of-band system kwarg，
        # 不切片，content[0].text guard + fallback）。host.last_input_token_count=0 在调用方。
        # strip_leading_system 对 Anthropic 无影响（system 走 out-of-band）。
        if messages is None or len(messages) < min_messages:
            return None
        summary_resp = await self.client.messages.create(
            model=model,
            max_tokens=max_output_tokens,
            system=system_persona,
            messages=self._with_summary_request(messages, instruction),
        )
        return (summary_resp.content[0].text
                if summary_resp.content and summary_resp.content[0].type == "text"
                else "No summary available.")

    async def stream(self, *, model, system, tools, messages, thinking_mode, callbacks):
        async def _do():
            max_output = _get_max_output_tokens(model)
            create_params: dict[str, Any] = {
                "model": model,
                "max_tokens": max_output if thinking_mode != "disabled" else 16384,
                "system": system,
                "tools": get_active_tool_definitions(tools, registry=self._registry),
                "messages": messages,
            }
            if thinking_mode in ("adaptive", "enabled"):
                create_params["thinking"] = {"type": "enabled", "budget_tokens": max_output - 1}

            thinking_parts: list[str] = []

            async with self.client.messages.stream(**create_params) as stream:
                async for event in stream:
                    if not hasattr(event, "type"):
                        continue
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            callbacks.spinner_stop()
                            callbacks.text_block(delta.text)
                        elif hasattr(delta, "thinking"):
                            thinking_parts.append(delta.thinking)
                            callbacks.spinner_stop()
                            callbacks.thinking_block(delta.thinking)

                final_message = await stream.get_final_message()

            final_message._nanocode_thinking = "".join(thinking_parts)
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message

        return await _with_retry(_do, on_retry=callbacks.retry)

    @staticmethod
    def _block_to_dict(block) -> dict:
        """Anthropic content block → plain dict for storage（移植自 core.block_to_dict，逐字一致）。"""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name,
                    "input": dict(block.input) if hasattr(block.input, "items") else block.input}
        return {"type": block.type}

    def complete(self, raw) -> Completion:
        # 逐字保留旧 anthropic 循环的完成整形（block_to_dict 建 assistant_message、从 content 提
        # tool_use、usage 取 .input/.output_tokens、stop_reason 取原生 .stop_reason）。
        tool_calls = [
            {"id": b.id, "name": b.name,
             "input": dict(b.input) if hasattr(b.input, "items") else b.input}
            for b in raw.content if b.type == "tool_use"
        ]
        return Completion(
            assistant_message={"role": "assistant",
                               "content": [self._block_to_dict(b) for b in raw.content]},
            tool_calls=tool_calls,
            usage=(raw.usage.input_tokens, raw.usage.output_tokens),
            stop_reason=getattr(raw, "stop_reason", None),
        )

    def tool_result_messages(self, results: list) -> list:
        # 逐字保留旧 anthropic 循环：一条批量 user 消息（allowed 带 toolLatencyMs，denied 带 is_error）。
        blocks: list[dict] = []
        for r in results:
            blk = {"type": "tool_result", "tool_use_id": r["tool_call_id"],
                   "content": r["content"], "toolName": r["name"]}
            if r.get("is_error"):
                blk["is_error"] = True
            else:
                blk["toolLatencyMs"] = r["latency_ms"]
            blocks.append(blk)
        if not blocks:
            return []
        return [({"role": "user", "content": blocks}, None)]


class OpenAIAdapter(ProviderAdapter):
    """OpenAI 兼容流式（移植自 engine.OpenAIBackendMixin._call_openai_stream，行为逐字一致）。
    system 在 messages[0]、不走 thinking——签名保持与 AnthropicAdapter 一致,忽略 system/thinking_mode。"""

    name = "openai"
    capture_api = "openai-completions"
    places_system_in_messages = True

    async def summarize(self, *, model, messages, system_persona, instruction,
                        max_output_tokens, min_messages, strip_leading_system=False):
        # 逐字保留旧 _compact_openai / _summarize_openai 体（in-band system messages[0]）。
        # **parity 雷 A**：strip_leading_system=True（compaction：caller 渲染前缀已带 system[0]）时切
        # messages[1:] 避免 double-place；=False（branch-summary：user-only transcript 未渲染 system）不切。
        # **parity 雷 B**：长度守卫下限 min_messages 由 caller 传（compaction +1 含 render system[0]）。
        # max_tokens：旧 _compact_openai / _summarize_openai 均不传——故 OpenAI 侧忽略 max_output_tokens
        # （byte-equivalent）。
        if messages is None or len(messages) < min_messages:
            return None
        body = messages[1:] if strip_leading_system else messages
        summary_resp = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_persona},
                *self._with_summary_request(body, instruction),
            ],
        )
        return summary_resp.choices[0].message.content or "No summary available."

    async def stream(self, *, model, system, tools, messages, thinking_mode, callbacks):
        async def _do():
            stream = await self.client.chat.completions.create(
                model=model,
                tools=_to_openai_tools(get_active_tool_definitions(tools, registry=self._registry)),
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
                    callbacks.spinner_stop()
                    callbacks.text_block(delta.content)
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

    def complete(self, raw) -> Completion:
        # 逐字保留旧 openai 循环的完成整形：assistant_message = choices[0].message，tool_calls 从
        # message.tool_calls 解析（仅 type==function；arguments json.loads，坏 JSON → {}），
        # stop_reason 取原生 finish_reason。usage：parity——旧 run_openai_turn 仅在 response.get("usage")
        # 为真时 add_usage / 落 usage 键，缺 usage 时 last_input_token_count 不动、树消息无 usage 键。
        # 故这里区分「缺 usage」(→ None) 与「真零」(→ (0,0))；core.py 据 None 守卫写回。真 adapter.stream()
        # 恒合成非空 usage（line 385），生产路径不受影响,差异只对省略 usage 的非合规 provider/mock 可见。
        choice = raw.get("choices", [{}])[0] if raw.get("choices") else {}
        message = choice.get("message", {})
        tool_calls: list[dict] = []
        for tc in (message.get("tool_calls") or []):
            if tc.get("type") != "function":
                continue
            try:
                inp = json.loads(tc["function"]["arguments"])
            except Exception:
                inp = {}
            tool_calls.append({"id": tc["id"], "name": tc["function"]["name"], "input": inp})
        u = raw.get("usage")
        return Completion(
            assistant_message=message,
            tool_calls=tool_calls,
            usage=((u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                   if u else None),
            stop_reason=choice.get("finish_reason"),
        )

    def tool_result_messages(self, results: list) -> list:
        # 逐字保留旧 openai 循环：每个结果一条 {"role":"tool",...}，per-call latency 经 record kwarg。
        # denied（无 latency）record 不带 latency_ms（=None）。
        return [({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]},
                 None if r.get("is_error") else r["latency_ms"])
                for r in results]


def _build_anthropic_client(*, api_key, api_base, anthropic_base_url):
    """构造 AsyncAnthropic（移植自 engine.__init__ 的 inline 分支，逐字一致）。SDK 未装 → None。"""
    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if anthropic_base_url:
        kwargs["base_url"] = anthropic_base_url
    try:
        import anthropic
    except ModuleNotFoundError:
        return None
    return anthropic.AsyncAnthropic(**kwargs)


def _build_openai_client(*, api_key, api_base, anthropic_base_url):
    """构造 AsyncOpenAI（移植自 engine.__init__ 的 inline 分支，逐字一致）。SDK 未装 → None。"""
    try:
        import openai
    except ModuleNotFoundError:
        return None
    return openai.AsyncOpenAI(base_url=api_base, api_key=api_key)


@dataclass(frozen=True)
class ProviderSpec:
    """provider 注册表条目（B1）：name + capture/system 标志 + adapter 类 + client 工厂。

    单一真源——engine 构造、capture api 串、system 放置规则都从这里读，新增 provider 只加一条。"""

    name: str
    capture_api: str
    places_system_in_messages: bool
    adapter_cls: type
    build_client: Callable[..., Any]


SPECS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        name="anthropic", capture_api="anthropic", places_system_in_messages=False,
        adapter_cls=AnthropicAdapter, build_client=_build_anthropic_client),
    "openai": ProviderSpec(
        name="openai", capture_api="openai-completions", places_system_in_messages=True,
        adapter_cls=OpenAIAdapter, build_client=_build_openai_client),
}


def resolve_provider_name(*, api_base: "str | None") -> str:
    """provider 名解析（= 旧 bool(api_base)）：openai-compatible base 非空 → openai，否则 anthropic。"""
    return "openai" if api_base else "anthropic"


def make_provider_adapter(*, provider: str, client, registry=None) -> ProviderAdapter:
    """按 provider name 查 SPECS 建 adapter（engine.__init__ 与子 agent 构造共用）。

    docs/24 Phase 4a：传入 agent 的 per-agent overlay registry，使 active-tool 过滤读其激活集。"""
    spec = SPECS[provider]
    return spec.adapter_cls(client, registry=registry)
