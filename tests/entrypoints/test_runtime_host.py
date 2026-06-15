"""docs/14 P1：RuntimeHost —— context() 动态绑定当前 thread、replace_thread dispose+rebind、
can_switch fail-closed 闸。"""

from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread
from nanocode.agent.engine import Agent
from nanocode.entrypoints.host import RuntimeHost


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def _host(sid="h"):
    a = _agent(sid)
    rt = AgentRuntime()
    t = rt._attach_agent(a)
    return a, rt, t, RuntimeHost(rt, t, registry=None)


def test_context_binds_current_thread_and_is_regenerated():
    a, rt, t, host = _host("h1")
    c1 = host.context()
    assert c1.thread.agent is a
    assert c1.thread is t
    # 每次新建、不缓存——替换 thread 后无需通知 handler
    c2 = host.context()
    assert c2 is not c1 and c2.thread.agent is a


def test_replace_thread_disposes_old_and_rebinds_registry():
    a1, rt, t1, host = _host("h2a")
    assert rt.thread(t1.thread_id) is t1
    a2 = _agent("h2b")
    t2 = rt._attach_agent(a2)
    host.replace_thread(t2)
    assert host.current_thread is t2
    assert rt.thread(t1.thread_id) is None       # 旧 thread 已注销
    assert rt.thread(t2.thread_id) is t2
    assert host.context().thread.agent is a2            # context 现在绑新 thread


def test_dispose_is_idempotent():
    a, rt, t, host = _host("h2c")
    t.dispose()
    t.dispose()                                  # 幂等，不抛
    assert rt.thread(t.thread_id) is None


def test_can_switch_allows_when_idle():
    a, rt, t, host = _host("h3")
    ok, reason = host.can_switch()
    assert ok is True and reason is None


def test_can_switch_blocks_on_running_turn():
    a, rt, t, host = _host("h4")

    class _Pending:
        def done(self):
            return False

    a._current_task = _Pending()                 # is_processing → True
    ok, reason = host.can_switch()
    assert ok is False and "turn" in reason


def test_can_switch_blocks_on_background_task():
    a, rt, t, host = _host("h5")
    a._background_tasks.add(object())
    ok, reason = host.can_switch()
    assert ok is False and "background" in reason


def test_can_switch_blocks_on_running_subagent():
    a, rt, t, host = _host("h6")

    class _Sub:
        status = "running"

    a.task_manager.list_subagents = lambda: [_Sub()]
    ok, reason = host.can_switch()
    assert ok is False and "sub-agent" in reason


# ─── P2 安全不变量：同一 Agent 跨 rebind 复用时 thread 身份/registry 正确性 ──────────────

def test_thread_id_snapshot_survives_in_place_session_mutation():
    """复刻 P2 rebind 形状（不用 P2 代码）：同一 Agent 原地改 session_id，old/new thread 复用它。

    若 RuntimeThread.thread_id 退回 live property，old.dispose() 会解析成 NEW sid 并误删 new
    （runtime.py docstring 所述 bug）——本测试锁定 snapshot 不变量，P2 依赖它。"""
    a = _agent("OLD")
    rt = AgentRuntime()
    old_t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    a.session_id = "NEW"                          # 模拟 in-place rebind 改 session_id
    new_t = RuntimeThread(rt, a, AgentSession(a))
    host = RuntimeHost(rt, old_t)
    host.replace_thread(new_t)                    # register(new) + dispose(old)
    assert host.current_thread is new_t
    assert rt.thread("NEW") is new_t              # 新 thread 在 registry
    assert rt.thread("OLD") is None               # 旧 thread 已注销
    assert old_t.thread_id == "OLD"               # 旧 thread 身份未跟随 mutation（snapshot）
    assert new_t.thread_id == "NEW"


def test_same_sid_rebind_does_not_evict_new_thread():
    """compare-and-delete：old/new 共享同一 session_id 时，old.dispose() 不得删掉 new 的 slot。"""
    a = _agent("SID")
    rt = AgentRuntime()
    old_t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    new_t = RuntimeThread(rt, a, AgentSession(a))   # 同 sid 的新 thread（重入/同 sid resume）
    host = RuntimeHost(rt, old_t)
    host.replace_thread(new_t)
    assert host.current_thread is new_t
    assert rt.thread("SID") is new_t                # new 存活，未被 old.dispose 误删


def test_disposed_thread_is_inert():
    """dispose 后 run 拒绝、cancel no-op（codex B2：stale 句柄复用同一 Agent 会写错 session）。"""
    import asyncio
    import pytest
    a, rt, t, host = _host("disp1")
    t.dispose()
    with pytest.raises(RuntimeError):
        asyncio.run(t.run("hi"))
    t.cancel()                                      # disposed 后 cancel no-op，不抛
