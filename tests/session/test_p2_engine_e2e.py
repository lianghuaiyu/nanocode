"""S1 message-end → 树 e2e：真实 turn 后 session.jsonl 含**干净**消息（注入是 render-time、不入树），
build_context/render 重现该 turn。取代 P2 的 _auto_save 双写测试（已改为 message-end 写入）。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.session.manager import SessionManager, session_file
from nanocode.session.render import ModelCtx, render

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
    a._mcp_initialized = True  # 跳过 MCP
    a.model = "claude-x"
    return a


def test_message_end_writes_clean_tree():
    a = _agent("s1e")

    async def fake_stream(on_tool_block_complete=None):
        return _FakeResp([_FakeBlock("text", text="hi there")])

    a._call_anthropic_stream = fake_stream
    asyncio.run(a.chat("hello"))

    assert session_file("s1e").exists()
    msgs = SessionManager.open("s1e").build_context().messages
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello"          # 干净：无注入文本进树
    payload = render(msgs, ANTH)["messages"]
    assert [m["role"] for m in payload] == ["user", "assistant"]


def test_message_end_tool_turn_records_full_round():
    a = _agent("s1t")
    calls = {"n": 0}

    async def fake_stream(on_tool_block_complete=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a._call_anthropic_stream = fake_stream
    asyncio.run(a.chat("list"))

    msgs = SessionManager.open("s1t").build_context().messages
    assert [m["role"] for m in msgs] == ["user", "assistant", "toolResult", "assistant"]
    # render 出合法 provider 序列（toolResult → anthropic user 消息）
    payload = render(msgs, ANTH)["messages"]
    assert [m["role"] for m in payload] == ["user", "assistant", "user", "assistant"]


def test_subagent_writes_child_tree_not_parent_tree():
    # docs/14 full-P6b + SessionLease：子 agent _tree_record 写自己的 child session（注入的 child 租约），
    # 不碰父 session。spawn 给子 agent 注入一把 child 写者租约（locked child mgr）。
    from nanocode.session.lease import SessionLease
    a = _agent("s1sub")
    a.is_sub_agent = True
    a._tree_session_id = "PARENT.s1sub"
    a._child_parent_session = {"sessionId": "PARENT", "entryId": None,
                               "taskId": "s1sub", "agentId": "s1sub"}
    a._session_mgr = SessionLease.open_or_create(
        a._tree_session_id, parent_session=a._child_parent_session).manager
    a._tree_record({"role": "user", "content": "x"})
    assert session_file("PARENT.s1sub").exists()       # 写到 child session
    assert not session_file("PARENT").exists()          # 不碰父 session
    assert not session_file("s1sub").exists()           # 也不写自身 session_id（已解耦到 child）


def test_s2_request_built_from_tree_not_flat_list():
    # 让扁平列表与树内容**不同**，证明 S2 的请求来自树（render(build_context)）而非扁平列表。
    from nanocode.session import tree as T
    a = _agent("s2req")
    mgr = SessionManager.create("s2req")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("FROM-TREE"))
    a._anthropic_messages = [{"role": "user", "content": "FROM-FLAT-STALE"}]
    req = a._build_request_messages()
    joined = str(req)
    assert "FROM-TREE" in joined and "FROM-FLAT" not in joined


def test_required_tree_record_reraises_on_write_failure(monkeypatch):
    # review medium：user 消息是 required 写——树是唯一权威、本轮请求从树渲染，写失败若被吞掉会向模型
    # 发缺失上下文。required=True 时 _tree_record 须重抛（fail loudly）；默认 best-effort 仍吞掉。
    import pytest
    from nanocode.session.lease import SessionLease
    a = _agent("reqw")
    a._session_mgr = SessionLease.open_or_create("reqw").manager
    def boom(*args, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(a._session_mgr, "append_message", boom)
    with pytest.raises(OSError):
        a._tree_record({"role": "user", "content": "hi"}, required=True)   # 必写 → 重抛
    a._tree_record({"role": "assistant", "content": "x"})                  # 默认 best-effort → 不抛
