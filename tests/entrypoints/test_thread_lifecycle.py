"""docs/14 P2：runtime-owned lifecycle —— /new (thread_new) 与 /resume <id> (thread_resume)
经 AgentRuntime 原子替换整组 Agent/AgentSession/RuntimeThread。

headline 修复：thread_resume 后 RuntimeHost.current_thread.session.context_builder.session_id
指向**新** session（unsafe-switch 时它 stale 在旧 session——见已删的 P0 characterization）。
"""

from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread
from nanocode.agent.engine import Agent
from nanocode.entrypoints.host import RuntimeHost
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


def _host(sid):
    a = _agent(sid)
    a._session_mgr = SessionManager.create(sid)
    rt = AgentRuntime()
    t = rt.register(RuntimeThread(rt, a, AgentSession(a)))
    return a, rt, t, RuntimeHost(rt, t, registry=None)


def test_thread_new_switches_to_empty_session_preserving_old():
    a, rt, t, host = _host("OLD")
    a._session_mgr.append_message(T.user_message("old-q"))
    new_t = rt.thread_new(host)
    assert host.current_thread is new_t
    assert new_t.agent is a                              # 同一 Agent，原地 rebind
    assert a.session_id != "OLD"                         # 切到新 mint 的 sid
    assert SessionManager.exists(a.session_id)           # 新空 session 已建
    assert not a._anthropic_messages                     # 新 session 上下文为空
    # registry：新 thread 在、旧 thread 注销
    assert rt.thread(new_t.thread_id) is new_t and rt.thread("OLD") is None
    # 旧 session 树保留（可 /resume 回去）
    assert any(e.type == T.MESSAGE for e in SessionManager.open("OLD").entries())


def test_thread_resume_reloads_target_and_rebinds_context_builder():
    a, rt, t, host = _host("CUR")
    a._session_mgr.append_message(T.user_message("current-q"))
    SessionManager.create("TGT").append_message(T.user_message("target-conversation"))
    new_t = rt.thread_resume(host, "TGT")
    assert new_t is not None and host.current_thread is new_t
    assert a.session_id == "TGT"
    assert "target-conversation" in str(a._anthropic_messages)
    assert "current-q" not in str(a._anthropic_messages)
    # headline 修复：新 AgentSession 的 context_builder 重绑到新 session（不再 stale 在 CUR）
    assert host.current_thread.session.context_builder.session_id == "TGT"


def test_thread_resume_unknown_session_returns_none():
    a, rt, t, host = _host("CUR2")
    assert rt.thread_resume(host, "does-not-exist-xyz") is None
    assert a.session_id == "CUR2"                         # 未切换


def test_thread_resume_migrates_legacy_session():
    # 无 canonical 树但有 legacy flat 快照 → thread_resume 先迁移再切。
    from nanocode.session.store import save_session
    save_session("LEG", {"metadata": {"id": "LEG"},
                         "anthropicMessages": [{"role": "user", "content": "legacy-msg"}]})
    a, rt, t, host = _host("CUR3")
    new_t = rt.thread_resume(host, "LEG")
    assert new_t is not None and a.session_id == "LEG"
    assert SessionManager.exists("LEG")                  # 迁移建了 canonical 树
    assert "legacy-msg" in str(a._anthropic_messages)
