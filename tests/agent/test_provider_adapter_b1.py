"""B1 provider seam parity anchors（docs/16）。

把 provider-specific 行为下沉到 ProviderAdapter 缝之下后，turn/压缩/摘要/系统提示词的调用点不再
分支 provider。这些测试钉住 B1 的等价契约（同一 model，两 provider）：
- adapter 三标志（name / capture_api / places_system_in_messages）由 provider spec 暴露；
- summarize（compaction + branch-summary）两路在 system 放置 / messages[1:] 切片 / 长度守卫 /
  fallback 上 byte-equivalent（**parity 雷 A/B**）；
- capture 串经 adapter.capture_api 选表（与旧 "openai"/"anthropic" + api 串一致）；
- AgentConfig.provider 显式覆盖 + resolve_provider_name 的 api_base 推断；
- Agent 暴露明确的 provider runtime config，不暴露 provider-specific client 兼容属性。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.providers import (
    AnthropicAdapter,
    OpenAIAdapter,
    SPECS,
    make_provider_adapter,
    resolve_provider_name,
)


def _agent(*, provider_name="anthropic", sid="b1"):
    kw = dict(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    if provider_name == "openai":
        kw.update(api_base="http://localhost:1/v1", model="gpt-test")
    return Agent(**kw)


# ── fake provider clients：记录最后一次请求，返回固定文本 ──────────────────────

class _FakeAnthropicMessages:
    def __init__(self, outer):
        self.outer = outer

    async def create(self, **kwargs):
        self.outer.last = kwargs

        class _Block:
            type = "text"
            text = "ANTHRO_SUMMARY"

        class _Resp:
            content = [_Block()]

        return _Resp()


class _FakeAnthropicClient:
    def __init__(self):
        self.last = None
        self.messages = _FakeAnthropicMessages(self)


class _FakeOpenAICompletions:
    def __init__(self, outer):
        self.outer = outer

    async def create(self, **kwargs):
        self.outer.last = kwargs

        class _Msg:
            content = "OAI_SUMMARY"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


class _FakeOpenAIChat:
    def __init__(self, outer):
        self.completions = _FakeOpenAICompletions(outer)


class _FakeOpenAIClient:
    def __init__(self):
        self.last = None
        self.chat = _FakeOpenAIChat(self)


# ── adapter 三标志 ───────────────────────────────────────────────────────────

def test_spec_flags_define_provider_projection():
    assert SPECS["anthropic"].capture_api == "anthropic"
    assert SPECS["anthropic"].places_system_in_messages is False
    assert SPECS["openai"].capture_api == "openai-completions"
    assert SPECS["openai"].places_system_in_messages is True


def test_adapter_class_flags():
    a = AnthropicAdapter(client=None)
    o = OpenAIAdapter(client=None)
    assert (a.name, a.capture_api, a.places_system_in_messages) == ("anthropic", "anthropic", False)
    assert (o.name, o.capture_api, o.places_system_in_messages) == ("openai", "openai-completions", True)


def test_resolve_provider_name_from_api_base():
    assert resolve_provider_name(api_base=None) == "anthropic"
    assert resolve_provider_name(api_base="") == "anthropic"
    assert resolve_provider_name(api_base="http://x/v1") == "openai"


def test_make_provider_adapter_by_name():
    assert isinstance(make_provider_adapter(provider="anthropic", client=None), AnthropicAdapter)
    assert isinstance(make_provider_adapter(provider="openai", client=None), OpenAIAdapter)


def test_agent_provider_attrs_track_adapter():
    a_anthropic = _agent()
    a_openai = _agent(provider_name="openai")
    assert a_anthropic.provider_runtime_config.name == "anthropic"
    assert a_openai.provider_runtime_config.name == "openai"
    assert a_anthropic._provider.name == "anthropic"
    assert a_anthropic._provider.capture_api == "anthropic"
    assert a_anthropic._provider.places_system_in_messages is False
    assert a_openai._provider.name == "openai"
    assert a_openai._provider.capture_api == "openai-completions"
    assert a_openai._provider.places_system_in_messages is True


# ── AgentConfig.provider 显式覆盖 ────────────────────────────────────────────

def test_explicit_provider_overrides_api_base_resolution():
    # 显式 provider 优先于 api_base 解析（同一 client 注入两侧均可工作）。
    a = Agent(api_key="t", session_id="ovr", provider="openai",
              api_base="http://x/v1", model="gpt-test", permission_mode="bypassPermissions")
    assert a._provider.name == "openai"
    b = Agent(api_key="t", session_id="ovr2", provider="anthropic",
              permission_mode="bypassPermissions")
    assert b._provider.name == "anthropic"


# ── summarize parity：compaction（含 messages[1:] 切片 + 长度守卫）─────────────

def test_compact_system_placement_and_slice_parity():
    """同一 prefix（OpenAI 渲染时带 system[0]），两 provider 的 compaction summarizer 等价：
    Anthropic system 走 out-of-band kwarg、不切片；OpenAI system 进 messages[0]、切 messages[1:]。"""
    a_an = _agent(sid="cmp_an")
    a_oai = _agent(provider_name="openai", sid="cmp_oai")
    fake_an = _FakeAnthropicClient()
    fake_oai = _FakeOpenAIClient()
    a_an._provider.client = fake_an
    a_oai._provider.client = fake_oai

    # anthropic：3 条裸 prefix（无 render system）；openai：同样 3 条业务消息 + render 注入的 system[0]。
    biz = [{"role": "user", "content": "u1"},
           {"role": "assistant", "content": "a1"},
           {"role": "user", "content": "u2"}]
    an_prefix = list(biz)
    oai_prefix = [{"role": "system", "content": "SYS"}] + biz

    s_an = asyncio.run(a_an._compact(an_prefix))
    s_oai = asyncio.run(a_oai._compact(oai_prefix))

    assert s_an == "ANTHRO_SUMMARY"
    assert s_oai == "OAI_SUMMARY"

    # Anthropic：out-of-band system kwarg；messages = prefix + summary 指令并入末条 user。
    assert fake_an.last["system"].startswith("You are a coding-session summarizer")
    assert fake_an.last["max_tokens"] == 8192
    an_msgs = fake_an.last["messages"]
    assert len(an_msgs) == 3                      # 不新增条目（指令并入末条 user）
    assert an_msgs[0] == {"role": "user", "content": "u1"}

    # OpenAI：in-band system[0]（persona），且 caller 渲染的 system[0] 被切掉 → 业务 3 条等价于 anthropic。
    oai_msgs = fake_oai.last["messages"]
    assert oai_msgs[0] == {"role": "system",
                           "content": fake_an.last["system"]}   # 同一 persona
    # 切片后业务消息 = 与 anthropic 一致的 3 条（指令并入末条 user）。
    assert oai_msgs[1:] == an_msgs
    # parity 雷 A：不得 double-place（无第二个 SYS）/ wrong-strip。
    assert all(not (m["role"] == "system" and m["content"] == "SYS") for m in oai_msgs)


def test_compact_length_guard_parity():
    """parity 雷 B：<3（anthropic）/ <4（openai，含 render system[0]）守卫原样保——None 不触 API。"""
    a_an = _agent(sid="g_an")
    a_oai = _agent(provider_name="openai", sid="g_oai")
    fake_an = _FakeAnthropicClient()
    fake_oai = _FakeOpenAIClient()
    a_an._provider.client = fake_an
    a_oai._provider.client = fake_oai

    two = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}]
    # anthropic：2 < 3 → None；openai：3 业务 + system[0] = 3 条 < 4 → None（含 system 的等价业务量同为 2）。
    assert asyncio.run(a_an._compact(two)) is None
    three_oai = [{"role": "system", "content": "SYS"}] + two
    assert asyncio.run(a_oai._compact(three_oai)) is None
    assert fake_an.last is None and fake_oai.last is None
    # last_input_token_count 在守卫短路时不被清零（与旧体一致）。
    a_an.last_input_token_count = 42
    asyncio.run(a_an._compact(two))
    assert a_an.last_input_token_count == 42


# ── summarize parity：branch summary（user-only transcript，**不**切片）────────

def test_branch_summary_no_slice_parity():
    a_an = _agent(sid="br_an")
    a_oai = _agent(provider_name="openai", sid="br_oai")
    fake_an = _FakeAnthropicClient()
    fake_oai = _FakeOpenAIClient()
    a_an._provider.client = fake_an
    a_oai._provider.client = fake_oai

    messages = [{"role": "user", "content": "Abandoned branch transcript:\n\nblah"}]
    asyncio.run(a_an._summarize(list(messages), "BRANCH_PROMPT"))
    asyncio.run(a_oai._summarize(list(messages), "BRANCH_PROMPT"))

    assert fake_an.last["system"] == "You summarize abandoned conversation branches for continuity."
    assert fake_an.last["max_tokens"] == 2048
    # 指令并入末条 user（唯一一条 user）→ 仍 1 条。
    assert len(fake_an.last["messages"]) == 1
    assert "BRANCH_PROMPT" in fake_an.last["messages"][0]["content"]

    oai_msgs = fake_oai.last["messages"]
    # parity 雷 A：branch transcript 未渲染 system，故 OpenAI **不**切片（保留全部业务消息）。
    assert oai_msgs[0]["role"] == "system"
    assert oai_msgs[1:] == fake_an.last["messages"]


def test_branch_summary_empty_returns_none():
    a_an = _agent(sid="be_an")
    a_oai = _agent(provider_name="openai", sid="be_oai")
    fake_an = _FakeAnthropicClient()
    fake_oai = _FakeOpenAIClient()
    a_an._provider.client = fake_an
    a_oai._provider.client = fake_oai
    assert asyncio.run(a_an._summarize([], "P")) is None
    assert asyncio.run(a_oai._summarize([], "P")) is None
    assert fake_an.last is None and fake_oai.last is None


# ── capture api 串经 adapter 元数据暴露（G2：capture 本体归 ②b，adapter 只暴露 capture_api）──

def test_capture_api_exposed_by_adapter():
    a_an = _agent(sid="cap_an")
    a_oai = _agent(provider_name="openai", sid="cap_oai")
    assert a_an._provider.capture_api == "anthropic"
    assert a_oai._provider.capture_api == "openai-completions"
    # G2：adapter 不再持 capture/neutral_stop_reason 方法——消息归一在 ②b（events.py 的 record
    # 路径调 session.capture），① adapter 不 import session/tools。
    assert not hasattr(a_an._provider, "capture")
    assert not hasattr(a_an._provider, "neutral_stop_reason")
    assert not hasattr(a_oai._provider, "capture")
