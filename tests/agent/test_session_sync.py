"""docs/15 Phase 2 STEP D：AgentSession 拥有 state↔tree 同步的契约。

验收（§7）：
- turn replay 从 canonical 树重建相同 AgentState（hydrate_state().project() == render(build_context())）；
- record_event 把 AgentEvent 落成正确的 session entry（消息族中立直 append,遥测族 _tree_event）；
- verify_turn_consistency 检测 inverse-orphan / leaf 漂移（§7.6,删 flat 后的 fail-loud 守门）。
"""

import asyncio

from nanocode.agent import events as E
from nanocode.agent.engine import Agent
from nanocode.session.agent import AgentSession
from nanocode.agent.state import AgentState
from nanocode.session import tree as T
from nanocode.session.lease import SessionLease
from nanocode.session.render import ModelCtx, render

from .._helpers import attach_runtime_agent

ANTH = ModelCtx("anthropic", "anthropic", "claude-x")


class _FakeBlock:
    def __init__(self, type="text", **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = _FakeUsage()


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def test_hydrate_state_rebuilds_request_from_tree():
    a = _agent("rbs1")

    async def fake(**_kw):
        return _FakeResp([_FakeBlock("text", text="hi there")])

    a._provider.stream = fake
    attach_runtime_agent(a)
    asyncio.run(a._chat_internal("hello"))

    sess = AgentSession(a)
    state = sess.hydrate_state()
    assert isinstance(state, AgentState)
    assert state.provider == "anthropic" and state.model == "claude-x"
    # rebuild 保真：state.project() 与直接 render(build_context()) 逐条相等
    built = a._session_mgr.build_context()
    direct = render(built.messages, ANTH, system_prompt=None)["messages"]
    assert state.project().messages == direct
    # docs/15 Phase 3 cutover：真实 turn 在末尾,前面是 session-context 包(proj + memory 指引,折成 user)。
    assert [m["role"] for m in built.messages[-2:]] == ["user", "assistant"]
    assert all(m["role"] == "user" for m in built.messages[:-2])
    # 干净 turn → 一致（注入是 leaf-affecting custom_message,leaf/orphan 不变量保持）
    assert sess.verify_turn_consistency() == []


def test_hydrate_tool_turn_rebuilds_and_consistent():
    a = _agent("rbs_tool")
    calls = {"n": 0}

    async def fake(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a._provider.stream = fake
    attach_runtime_agent(a)
    asyncio.run(a._chat_internal("list"))
    sess = AgentSession(a)
    built = a._session_mgr.build_context()
    # docs/15 Phase 3 cutover：真实 round 在末尾,前面是 session-context 包。
    assert [m["role"] for m in built.messages[-4:]] == ["user", "assistant", "toolResult", "assistant"]
    assert all(m["role"] == "user" for m in built.messages[:-4])
    # tool turn 后无孤儿、leaf 正确
    assert sess.verify_turn_consistency() == []
    # state.project() 仍等于直接 render
    assert sess.hydrate_state().project().messages == render(built.messages, ANTH, system_prompt=None)["messages"]


def test_verify_detects_inverse_orphan():
    a = _agent("rbs2")
    a._session_mgr = SessionLease.open_or_create("rbs2").manager
    # toolResult 无对应 toolCall → inverse-orphan
    a._session_mgr.append_message(T.tool_result_message(tool_call_id="ghost", tool_name="x", content="r"))
    issues = AgentSession(a).verify_turn_consistency()
    assert any("inverse-orphan" in i for i in issues)


def test_record_event_writes_correct_entries():
    a = _agent("rbs3")
    a._session_mgr = SessionLease.open_or_create("rbs3").manager
    sess = AgentSession(a)

    sess.record_event(E.UserMessageAccepted(text="hi"))
    am = T.assistant_message([T.text_block("ok")], provider="anthropic", api="anthropic",
                             model="claude-x", stop_reason="stop", usage={"inputTokens": 3, "outputTokens": 1})
    sess.record_event(E.AssistantMessageCompleted(message=am, text="ok", thinking="", tool_uses=[],
                                                   stop_reason="stop", usage=None, latency_ms=12))
    assert sess.record_event(E.ContextInjected(custom_type="memory", content="REMEMBER")) is True
    sess.record_event(E.LlmRequestPrepared(model="claude-x", message_count=2, messages_chars=100))
    sess.record_event(E.TurnCompleted(input_tokens=10, output_tokens=5, turns=1))

    types = [e.type for e in a._session_mgr.entries()]
    assert types.count(T.MESSAGE) == 2          # user + assistant
    assert T.CUSTOM_MESSAGE in types
    assert T.LLM_REQUEST in types
    assert T.TURN_END in types
    # 中立 assistant 直接 append（capture 不参与）：toolCall 不会被吞（此处验证 text 折出）
    built = a._session_mgr.build_context()
    assert any(m.get("role") == "assistant" for m in built.messages)
    assert sess.verify_turn_consistency() == []


def test_record_event_assistant_with_toolcall_survives():
    # 关键回归：中立 assistant 的 toolCall block 经 record_event 直 append,不被 capture 吞掉。
    a = _agent("rbs4")
    a._session_mgr = SessionLease.open_or_create("rbs4").manager
    sess = AgentSession(a)
    sess.record_event(E.UserMessageAccepted(text="go"))
    am = T.assistant_message([T.tool_call_block("t1", "read_file", {"p": "a"})],
                             provider="anthropic", api="anthropic", model="claude-x", stop_reason="toolUse")
    sess.record_event(E.AssistantMessageCompleted(message=am, text="", thinking="",
                                                   tool_uses=[{"id": "t1"}], stop_reason="toolUse",
                                                   usage=None, latency_ms=5))
    tr = T.tool_result_message(tool_call_id="t1", tool_name="read_file", content="file a")
    sess.record_event(E.ToolResultCompleted(message=tr, tool="read_file", tool_use_id="t1",
                                            content="file a", is_error=False, latency_ms=7))
    built = a._session_mgr.build_context()
    asst = [m for m in built.messages if m["role"] == "assistant"]
    assert asst and any(b.get("type") == "toolCall" and b.get("id") == "t1" for b in asst[0]["content"])
    # toolCall 有对应 toolResult → 无 orphan
    assert sess.verify_turn_consistency() == []
