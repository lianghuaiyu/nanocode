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
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


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
    new_sid = a.session_id
    assert host.current_thread is new_t
    assert new_t.agent is a                              # 同一 Agent，原地 rebind
    assert new_sid != "OLD"                              # 切到新 mint 的 sid
    assert not SessionManager.exists(new_sid)            # Pi 对齐：顶层空 session 首个 assistant 前不落盘
    assert a.agent_session.build_request_messages() == []   # 新 session 上下文为空
    a.agent_session.record_provider_messages({"role": "user", "content": "new-q"})
    assert not SessionManager.exists(new_sid)
    a.agent_session.record_provider_messages(
        {"role": "assistant", "content": [{"type": "text", "text": "new-a"}]})
    assert SessionManager.exists(new_sid)
    # registry：新 thread 在、旧 thread 注销
    assert rt.thread(new_t.thread_id) is new_t and rt.thread("OLD") is None
    # 旧 session 树保留（可 /resume 回去）
    assert any(e.type == T.MESSAGE for e in SessionManager.open("OLD").entries())


def test_thread_resume_reloads_target_and_rebinds_session():
    a, rt, t, host = _host("CUR")
    a._session_mgr.append_message(T.user_message("current-q"))
    SessionManager.create("TGT").append_message(T.user_message("target-conversation"))
    seen = []
    host.current_thread.subscribe(seen.append)
    new_t = rt.thread_resume(host, "TGT")
    assert new_t is not None and host.current_thread is new_t
    assert a.session_id == "TGT"
    assert "target-conversation" in str(a.agent_session.build_request_messages())
    assert "current-q" not in str(a.agent_session.build_request_messages())
    # docs/14 P2/P7：新 AgentSession 绑新 session（SessionContextBuilder 已退役，不再有 stale 缓存）
    assert host.current_thread.session.session_id == "TGT"
    assert seen[0]["type"] == "session_shutdown"
    assert seen[0]["event"]["reason"] == "resume"


def test_thread_resume_rebuilds_cwd_bound_services(tmp_path):
    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    a = _agent("CWDA")
    a._session_mgr = SessionManager.create("CWDA", cwd=str(cwd_a))
    rt = AgentRuntime()
    t = rt._attach_agent(a)
    host = RuntimeHost(rt, t, registry=None)
    SessionManager.create("CWDB", cwd=str(cwd_b)).append_message(T.user_message("target"))

    new_t = rt.thread_resume(host, "CWDB")

    assert new_t is not None
    assert host.current_thread.services.cwd == str(cwd_b.resolve())
    assert a._runtime_services.cwd == str(cwd_b.resolve())
    assert a._memory_service is host.current_thread.services.memory_service


def test_thread_resume_unknown_session_returns_none():
    a, rt, t, host = _host("CUR2")
    assert rt.thread_resume(host, "does-not-exist-xyz") is None
    assert a.session_id == "CUR2"                         # 未切换


def test_thread_resume_treeless_sid_returns_none_no_tree_creation():
    # docs/16 C-3：canonical 树是唯一 resume 权威——无 session.jsonl 的 sid → thread_resume 返回
    # None、不切换、绝不顺手建空树（否则会 clobber 同名 sid 的未来恢复语义）。
    a, rt, t, host = _host("CUR3")
    new_t = rt.thread_resume(host, "TREELESS")
    assert new_t is None                                 # 无 canonical 树 → 不可 resume
    assert a.session_id == "CUR3"                        # 未切换
    assert not SessionManager.exists("TREELESS")         # 未建树
