"""docs/15 Phase 0/1 STEP B：ProviderAdapter 流式 parse 的 characterization 测试。

旧 `_call_*_stream` 从未被单测（e2e 测试都在该层 mock 掉）。STEP C 把循环上移前,这里用 fake SDK
client 钉住两个 adapter 的 streaming parse + callback 触发（早执行 tool_block / text / thinking /
spinner / finish_reason / usage 组装）。
"""

import asyncio

from nanocode.agent.providers import AnthropicAdapter, OpenAIAdapter, StreamCallbacks


# ─── Anthropic fake SDK stream ───────────────────────────────────────────────
class _FE:
    def __init__(self, type, index=0, content_block=None, delta=None):
        self.type = type
        self.index = index
        if content_block is not None:
            self.content_block = content_block
        if delta is not None:
            self.delta = delta


class _CB:
    def __init__(self, type, id=None, name=None):
        self.type = type
        self.id = id
        self.name = name


class _Delta:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FinalBlock:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Final:
    def __init__(self, content):
        self.content = content


class _FakeAnthropicStream:
    def __init__(self, events, final):
        self._events, self._final = events, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for e in self._events:
            yield e

    async def get_final_message(self):
        return self._final


class _FakeAnthropicClient:
    def __init__(self, stream_obj):
        self._s = stream_obj
        self.messages = self

    def stream(self, **kw):
        self.last_kw = kw
        return self._s


def test_anthropic_adapter_parses_text_thinking_tool():
    events = [
        _FE("content_block_start", index=1, content_block=_CB("tool_use", id="t1", name="read_file")),
        _FE("content_block_delta", index=0, delta=_Delta(text="hel")),
        _FE("content_block_delta", index=0, delta=_Delta(text="lo")),
        _FE("content_block_delta", index=2, delta=_Delta(thinking="reason")),
        _FE("content_block_delta", index=1, delta=_Delta(partial_json='{"p":')),
        _FE("content_block_delta", index=1, delta=_Delta(partial_json='"a"}')),
        _FE("content_block_stop", index=0),
        _FE("content_block_stop", index=2),
        _FE("content_block_stop", index=1),
    ]
    final = _Final([_FinalBlock("text", text="hello"),
                    _FinalBlock("thinking", thinking="reason"),
                    _FinalBlock("tool_use", id="t1", name="read_file", input={"p": "a"})])
    ad = AnthropicAdapter(_FakeAnthropicClient(_FakeAnthropicStream(events, final)))
    texts, thinks, spins = [], [], []
    cb = StreamCallbacks(spinner_stop=lambda: spins.append(1), text_block=texts.append,
                         thinking_block=thinks.append)
    res = asyncio.run(ad.stream(model="claude-x", system="S", tools=[], messages=[],
                                thinking_mode="disabled", callbacks=cb))
    assert texts == ["hel", "lo"]
    assert thinks == ["reason"]
    assert spins  # spinner stopped before first text/thinking block
    assert res._nanocode_thinking == "reason"
    assert all(b.type != "thinking" for b in res.content)   # thinking 过滤出 final content


def test_anthropic_adapter_streams_text_thinking_only():
    # B2-a：adapter 流中不再解析 tool_use/不再 fire tool_block（早执行已去除）；
    # tool_calls 在流完后由 complete() 从 final_message.content 派生。此处只钉 text/thinking 流式。
    events = [
        _FE("content_block_delta", index=0, delta=_Delta(text="hi")),
        _FE("content_block_stop", index=0),
    ]
    ad = AnthropicAdapter(_FakeAnthropicClient(_FakeAnthropicStream(events, _Final([]))))
    texts = []
    asyncio.run(ad.stream(model="claude-x", system=None, tools=[], messages=[],
                          thinking_mode="disabled", callbacks=StreamCallbacks(text_block=texts.append)))
    assert texts == ["hi"]


# ─── OpenAI fake SDK stream ──────────────────────────────────────────────────
class _OAIFn:
    def __init__(self, name=None, arguments=None):
        self.name, self.arguments = name, arguments


class _OAITC:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index, self.id = index, id
        self.function = _OAIFn(name, arguments)


class _OAIDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _OAIChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta, self.finish_reason = delta, finish_reason


class _OAIUsage:
    def __init__(self, p, c):
        self.prompt_tokens, self.completion_tokens = p, c


class _OAIChunk:
    def __init__(self, choices=None, usage=None):
        self.choices, self.usage = choices or [], usage


class _FakeOAIStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for c in self._chunks:
            yield c


class _FakeOAIClient:
    def __init__(self, stream):
        self._s = stream
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.last_kw = kw
        return self._s


def test_openai_adapter_assembles_text_and_tool_calls():
    chunks = [
        _OAIChunk(choices=[_OAIChoice(_OAIDelta(content="he"))]),
        _OAIChunk(choices=[_OAIChoice(_OAIDelta(content="llo"))]),
        _OAIChunk(choices=[_OAIChoice(_OAIDelta(tool_calls=[_OAITC(0, id="t1", name="run", arguments='{"x":')]))]),
        _OAIChunk(choices=[_OAIChoice(_OAIDelta(tool_calls=[_OAITC(0, arguments="1}")]), finish_reason="tool_calls")]),
        _OAIChunk(usage=_OAIUsage(7, 3)),
    ]
    ad = OpenAIAdapter(_FakeOAIClient(_FakeOAIStream(chunks)))
    texts = []
    res = asyncio.run(ad.stream(model="gpt-x", system=None, tools=[], messages=[],
                                thinking_mode="disabled", callbacks=StreamCallbacks(text_block=texts.append)))
    assert texts == ["he", "llo"]
    msg = res["choices"][0]["message"]
    assert msg["content"] == "hello"
    assert msg["tool_calls"][0]["function"] == {"name": "run", "arguments": '{"x":1}'}
    assert res["choices"][0]["finish_reason"] == "tool_calls"
    assert res["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}


def test_openai_adapter_no_tool_calls_finish_stop_default():
    chunks = [_OAIChunk(choices=[_OAIChoice(_OAIDelta(content="hi"))])]
    ad = OpenAIAdapter(_FakeOAIClient(_FakeOAIStream(chunks)))
    res = asyncio.run(ad.stream(model="gpt-x", system=None, tools=[], messages=[],
                                thinking_mode="disabled", callbacks=StreamCallbacks()))
    msg = res["choices"][0]["message"]
    assert msg["content"] == "hi"
    assert msg["tool_calls"] is None
    assert res["choices"][0]["finish_reason"] == "stop"      # 缺省 stop


# ─── adapter provider 元数据（G2：capture/neutral_stop_reason 已归 ②b，不再在 adapter 上）──────
def test_adapter_provider_metadata():
    a = AnthropicAdapter(None)
    assert a.name == "anthropic"
    assert a.capture_api == "anthropic"
    assert a.places_system_in_messages is False
    # G2：adapter 不再持 capture/neutral_stop_reason（消息归一属 ②b harness，由 events.py 的
    # record 路径调 session.capture；adapter 不 import session/tools）。
    assert not hasattr(a, "capture")
    assert not hasattr(a, "neutral_stop_reason")

    o = OpenAIAdapter(None)
    assert o.name == "openai"
    assert o.capture_api == "openai-completions"
    assert o.places_system_in_messages is True
