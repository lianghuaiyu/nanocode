"""docs/14 §6a：per-session 单写者锁（fcntl.flock）。第二个 writer fail-closed（SessionBusyError）；
read-only 打开不持锁；rebind/thread_resume 到被占用 session → busy（_apply_control 转 --fork）。"""

import pytest

from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread
from nanocode.agent.engine import Agent
from nanocode.entrypoints.host import RuntimeHost
from nanocode.session.manager import SessionManager
from nanocode.session.tree import SessionBusyError


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


def _host(sid):
    a = _agent(sid)
    a._session_mgr = SessionManager.create(sid)      # 当前 session 不持锁（测试构造）
    rt = AgentRuntime()
    t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    return a, rt, t, RuntimeHost(rt, t, registry=None)


def test_lock_excludes_second_writer():
    m1 = SessionManager.create("lk1", lock=True)
    with pytest.raises(SessionBusyError):
        SessionManager.open("lk1", lock=True)        # 第二写者 fail-closed
    m1.close()
    m2 = SessionManager.open("lk1", lock=True)        # 释放后可再取
    assert m2.locked
    m2.close()


def test_readonly_open_does_not_lock():
    m1 = SessionManager.create("lk2", lock=True)
    ro = SessionManager.open("lk2")                  # 无 lock 参数 → 不持锁、不冲突
    assert not ro.locked and ro.get_leaf() is None
    m1.close()


def test_thread_resume_to_busy_session_raises_busy():
    # 目标被另一 writer 持锁 → rebind pre-flight 取锁失败 → SessionBusyError（旧 session 不动）。
    a, rt, t, host = _host("curlk")
    holder = SessionManager.create("busytgt", lock=True)   # 模拟另一进程持锁
    with pytest.raises(SessionBusyError):
        rt.thread_resume(host, "busytgt")
    assert a.session_id == "curlk"                   # 未切换（fail-closed）
    holder.close()


def test_rebind_releases_old_lock_and_holds_new():
    a, rt, t, host = _host("oldlk")
    # 给当前 session 一把锁（模拟 writer），rebind 应释放它并锁住新 session
    a._session_mgr = SessionManager.open("oldlk", lock=True)
    SessionManager.create("newlk")                   # 目标存在、未被占
    rt.thread_resume(host, "newlk")
    assert a.session_id == "newlk" and a._session_mgr.locked
    # 旧 session 锁已释放 → 可再次取
    again = SessionManager.open("oldlk", lock=True)
    assert again.locked
    again.close()


def test_rebind_corrupt_new_session_does_not_leak_lock():
    # P6 review #1：新 session build_context 抛错（leaf 指向不存在 entry）时，pre-flight 取的新锁
    # 必须释放——否则同进程重试 /resume 自锁死。
    import json
    import pytest
    from nanocode.session import tree as T
    from nanocode.session.manager import session_file
    from nanocode.session.tree import SessionTreeError
    mgr = SessionManager.create("corruptlk")
    mgr.append_message(T.user_message("hi"))
    # 直接追加一条指向不存在 id 的 leaf entry（绕过 set_leaf 校验）→ get_branch 会抛
    with session_file("corruptlk").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "id": "ent_leafbad", "parentId": None, "sessionId": "corruptlk",
                            "type": "leaf", "timestamp": "t", "data": {"targetId": "ent_NOPE"}}) + "\n")
    a, rt, t, host = _host("curcorrupt")
    with pytest.raises(SessionTreeError):
        rt.thread_resume(host, "corruptlk")
    assert a.session_id == "curcorrupt"                  # 未切换
    again = SessionManager.open("corruptlk", lock=True)   # 锁未泄漏 → 可再取
    assert again.locked
    again.close()
