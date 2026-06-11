"""docs/15 Phase 0 契约：AgentState = SessionManager.build_context() 的可丢弃投影。

验收（§0/§6 硬不变量的可执行契约）：
- 中立 branch 能 hydrate 成 AgentState；
- AgentState 能 render 成 Anthropic / OpenAI 请求；
- 不需要任何 provider-specific durable messages（同一中立列表可渲染任一 provider）。
"""

from nanocode.agent.state import AgentState, ProviderProjection, provider_api
from nanocode.session import capture, tree
from nanocode.session.manager import SessionManager
from nanocode.session.render import ModelCtx, render

ANTH = ModelCtx(provider="anthropic", api="anthropic", model_id="claude-x")
OAI = ModelCtx(provider="openai", api="openai-completions", model_id="gpt-x")

ANTH_LIVE = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"p": "a"}},
    ]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file a"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
]


def _mgr_with(live, provider, model):
    mgr = SessionManager.create(cwd="/tmp")
    for neutral in capture.capture_provider_messages(live, provider, model=model):
        mgr.append_message(neutral)
    return mgr


def test_provider_api_helper():
    assert provider_api("openai") == "openai-completions"
    assert provider_api("anthropic") == "anthropic"


def test_hydrate_messages_are_neutral_not_provider_shaped():
    built = _mgr_with(ANTH_LIVE, "anthropic", "claude-x").build_context()
    state = AgentState.hydrate(built, provider="anthropic", model="claude-x")
    # 中立 Message[]，roles 只有 user/assistant/toolResult；toolResult 是独立消息（未并进 user）
    assert state.messages == built.messages
    assert {m["role"] for m in state.messages} <= {"user", "assistant", "toolResult"}
    assert any(m["role"] == "toolResult" for m in state.messages)
    # 中立 assistant 用 "toolCall" block（非 provider 的 "tool_use"）
    asst = [m for m in state.messages if m["role"] == "assistant"]
    blk_types = {b.get("type") for m in asst for b in m.get("content", [])}
    assert "toolCall" in blk_types and "tool_use" not in blk_types


def test_hydrate_reconstructs_scalar_provider_model():
    # 末条 assistant 记录 provider=anthropic/model=claude-x → scalar 折叠出,优先于传入默认
    built = _mgr_with(ANTH_LIVE, "anthropic", "claude-x").build_context()
    state = AgentState.hydrate(built, provider="openai", model="WRONG")
    assert state.provider == "anthropic"   # scalar 胜出（resume 忠实）
    assert state.model == "claude-x"
    assert state.api == "anthropic"


def test_project_matches_direct_render_anthropic():
    built = _mgr_with(ANTH_LIVE, "anthropic", "claude-x").build_context()
    state = AgentState.hydrate(built, provider="anthropic", model="claude-x", system_prompt="SYS")
    proj = state.project()
    assert isinstance(proj, ProviderProjection)
    assert proj.system == "SYS"            # anthropic：system out-of-band
    direct = render(built.messages, ANTH, system_prompt=None)
    assert proj.messages == direct["messages"]
    # 渲染后 toolResult 已并进 anthropic 的 user 消息（不再有独立 toolResult role）
    assert all(m["role"] in ("user", "assistant") for m in proj.messages)


def test_no_durable_provider_messages_same_neutral_renders_either():
    """同一份中立 messages 既能渲染成 anthropic 也能渲染成 openai —— 证明不需要 durable provider list。"""
    built = _mgr_with(ANTH_LIVE, "anthropic", "claude-x").build_context()
    neutral = built.messages
    anth = AgentState(messages=list(neutral), provider="anthropic", api="anthropic",
                      model="claude-x", system_prompt="S")
    oai = AgentState(messages=list(neutral), provider="openai", api="openai-completions",
                     model="gpt-x", system_prompt="S")
    pa, po = anth.project(), oai.project()
    # anthropic：system out-of-band；openai：system 进 messages[0]
    assert pa.system == "S"
    assert po.system is None
    assert po.messages[0] == {"role": "system", "content": "S"}
    # openai assistant 用 tool_calls；anthropic 用 content block tool_use —— 都从同一中立列表渲染
    assert any(m.get("tool_calls") for m in po.messages if m["role"] == "assistant")
    assert any(b.get("type") == "tool_use"
               for m in pa.messages if m["role"] == "assistant"
               for b in (m["content"] if isinstance(m["content"], list) else []))


def test_hydrate_empty_branch_is_empty_state():
    built = SessionManager.create(cwd="/tmp").build_context()
    state = AgentState.hydrate(built, provider="anthropic", model="claude-x")
    assert state.messages == []
    assert state.project().messages == []
