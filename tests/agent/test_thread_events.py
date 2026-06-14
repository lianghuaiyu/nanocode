"""docs/16 #4（EVENT-P2）：RuntimeThread typed 事件 push 流。

- subscribe(listener) → unsubscribe；信封 {thread_id, session_id, seq, type, event}；
- events() = ring buffer 近期快照（防膨胀）；
- listener 异常 fire-and-forget（不影响 turn 与其余订阅者）；
- rebind 发 session_switch 边界（旧/新 thread 都收到）；
- 信封绝不携带 tree entry id（docs/12 boundary 5）。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.runtime import AgentRuntime


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


def _tool_turn_agent(sid):
    a = _agent(sid)
    calls = {"n": 0}

    async def fake(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a._provider.stream = fake
    return a


# ─── push 流：订阅 + 快照 + 词表覆盖 ──────────────────────────────────────────

def test_turn_pushes_typed_envelopes_and_events_snapshot_matches():
    a = _tool_turn_agent("push1")
    thread = AgentRuntime().adopt(a)
    received = []
    unsubscribe = thread.subscribe(received.append)
    asyncio.run(thread.run("hello"))

    assert received == thread.events()                      # 快照 = 实时推送
    kinds = [env["type"] for env in received]
    # turn 边界 / 请求 / 消息族 / 工具 / 权限全覆盖
    for expected in ("user_message_accepted", "llm_request_prepared", "assistant_message_completed",
                     "tool_call_requested", "tool_call_authorized", "tool_result_observed",
                     "tool_result_completed", "turn_completed"):
        assert expected in kinds, f"missing {expected}"
    # 信封形状 + 单调 seq + 不泄露 tree entry id
    seqs = [env["seq"] for env in received]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for env in received:
        assert set(env) == {"thread_id", "session_id", "seq", "type", "event"}
        assert env["thread_id"] == thread.thread_id and env["session_id"] == a.session_id
        assert "ent_" not in str(env)                       # docs/12 boundary 5：entry id 不外泄
    unsubscribe()


def test_unsubscribe_stops_delivery_and_is_idempotent():
    a = _agent("push2")
    thread = AgentRuntime().adopt(a)
    got = []
    unsub = thread.subscribe(got.append)
    thread.push_boundary("session_switch", from_session="x", to_session="y")
    assert len(got) == 1
    unsub()
    unsub()                                                  # 幂等
    thread.push_boundary("session_switch", from_session="y", to_session="z")
    assert len(got) == 1                                     # 退订后不再投递
    assert len(thread.events()) == 2                         # 快照仍累积


def test_listener_exception_is_fire_and_forget():
    a = _agent("push3")
    thread = AgentRuntime().adopt(a)
    good = []

    def bad(_env):
        raise RuntimeError("subscriber bug")

    thread.subscribe(bad)
    thread.subscribe(good.append)
    thread.push_boundary("session_switch", from_session="a", to_session="b")
    assert len(good) == 1                                    # 坏订阅者不拖垮其余订阅者


def test_ring_buffer_caps_snapshot():
    a = _agent("push4")
    thread = AgentRuntime().adopt(a)
    for i in range(thread.EVENT_LOG_MAX + 50):
        thread.push_boundary("session_switch", from_session=str(i), to_session=str(i + 1))
    evs = thread.events()
    assert len(evs) == thread.EVENT_LOG_MAX                  # 防膨胀：只留近期
    assert evs[-1]["seq"] == thread.EVENT_LOG_MAX + 50       # seq 单调，未被 ring 重置


def test_dispose_detaches_tap_from_agent():
    a = _agent("push5")
    rt = AgentRuntime()
    thread = rt.adopt(a)
    assert thread._agent_tap in a._event_subscribers
    thread.dispose()
    assert thread._agent_tap not in a._event_subscribers     # disposed thread 不再累积事件


def test_rebind_emits_session_switch_boundary():
    # _switch_via_rebind：旧 thread 订阅者得知流被切走；新 thread 的流以 session_switch 开篇。
    from nanocode.session.manager import SessionManager

    class _Host:
        def __init__(self, thread):
            self.current_thread = thread
        def replace_thread(self, t):
            old, self.current_thread = self.current_thread, t
            old.dispose()

    a = _agent("swold")
    a._session_mgr = SessionManager.create("swold")
    rt = AgentRuntime()
    old_thread = rt.adopt(a)
    old_seen = []
    old_thread.subscribe(old_seen.append)

    new_thread = rt.thread_new(_Host(old_thread))
    # Pi-style replacement：旧 thread 先发 shutdown 边界，rebind 提示作为 NoticeRaised，
    # 随后发 session_switch，最后旧 wrapper 被 invalidated。
    assert [e["type"] for e in old_seen] == [
        "session_shutdown", "notice_raised", "session_switch", "thread_invalidated"
    ]
    assert old_seen[0]["event"]["reason"] == "new"
    assert old_seen[2]["event"]["from_session"] == "swold"
    assert new_thread.events()[0]["type"] == "session_switch"
    assert new_thread.events()[0]["event"]["to_session"] == a.session_id
