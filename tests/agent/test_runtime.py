"""P4：AgentRuntime / RuntimeThread / TurnResult / ApprovalManager（in-process facade）。

重点是不可回归契约：取消经 abort()、status 在 await 后读 _aborted 映射、final_response
从 agent 的 emit 流派生（docs/17 Phase 0：agent.final_text()）、ApprovalManager 装两条审批通道。
"""

from __future__ import annotations

import asyncio
import json

from nanocode.agent.engine import Agent
from nanocode.runtime import AgentRuntime, RuntimeThread, TurnResult, ApprovalManager, AgentConfig
from nanocode.agent.events import NoticeRaised


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="p4sid", **kw)


def test_agent_config_build_agent_applies_fields():
    cfg = AgentConfig(api_key="test", model="claude-x", permission_mode="bypassPermissions",
                      max_turns=7, session_id="cfgsid")
    a = cfg.build_agent()
    assert isinstance(a, Agent)
    assert a.model == "claude-x" and a.session_id == "cfgsid"
    assert a.max_turns == 7 and a.permission_mode == "bypassPermissions"
    assert a.use_openai is False                      # 无 api_base → anthropic


def test_agent_config_api_base_selects_openai():
    cfg = AgentConfig(api_key="test", api_base="https://x/v1", session_id="cfgoa",
                      permission_mode="bypassPermissions")
    a = cfg.build_agent()
    assert a.use_openai is True


def test_thread_start_builds_and_registers_thread():
    rt = AgentRuntime()
    cfg = AgentConfig(api_key="test", session_id="tsid", permission_mode="bypassPermissions",
                      )
    th = rt.thread_start(cfg)
    assert isinstance(th, RuntimeThread)
    assert th.thread_id == "tsid"
    assert rt.thread("tsid") is th
    assert th._agent.model == cfg.model
    th.release_lease()


def test_thread_start_new_session_uses_config_cwd(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    rt = AgentRuntime()
    cfg = AgentConfig(api_key="test", session_id="cwdstart", permission_mode="bypassPermissions",
                      cwd=str(cwd))
    th = rt.thread_start(cfg)
    try:
        assert th.services.cwd == str(cwd.resolve())
        assert th.readonly_session()._cwd() == str(cwd.resolve())
    finally:
        th.release_lease()



def test_runtime_does_not_expose_bare_agent_adopt_entrypoint():
    assert not hasattr(AgentRuntime(), "adopt")


def test_attach_agent_internal_registers_thread():
    rt = AgentRuntime()
    a = _agent()
    th = rt._attach_agent(a)
    assert isinstance(th, RuntimeThread)
    assert th.thread_id == a.session_id
    assert rt.thread(a.session_id) is th
    assert th in rt.threads()


def test_run_returns_turnresult_completed_with_tokens():
    rt = AgentRuntime()
    a = _agent()

    async def fake_chat(prompt):
        a.total_input_tokens += 10
        a.total_output_tokens += 4

    th = rt._attach_agent(a)
    th._session.run_turn = fake_chat        # docs/16 #3c：turn 实现在 AgentSession.run_turn
    res = asyncio.run(th.run("hi"))
    assert isinstance(res, TurnResult)
    assert res.status == "completed"
    assert res.input_tokens == 10 and res.output_tokens == 4
    assert res.thread_id == a.session_id


def test_run_maps_aborted_to_cancelled_status():
    """run_turn 把取消吞成 _aborted=True 并正常返回——run() 必须在 await 后读 _aborted。"""
    rt = AgentRuntime()
    a = _agent()

    async def aborted_chat(prompt):
        a._aborted = True   # 模拟取消被吞

    th = rt._attach_agent(a)
    th._session.run_turn = aborted_chat
    res = asyncio.run(th.run("x"))
    assert res.status == "cancelled"   # 不是 completed


def test_cancel_delegates_to_abort_order():
    """cancel 必须委托 agent.abort()（先置 _aborted 再 cancel task），不可只 cancel task。"""
    rt = AgentRuntime()
    a = _agent()
    calls = []
    a.abort = lambda: calls.append("abort")
    th = rt._attach_agent(a)
    th.cancel()
    assert calls == ["abort"]


def test_thread_status_snapshot_exposes_session_state():
    """docs/17 Phase 5a：status() 是客户端（footer/RPC）读会话状态的稳定 API，不跨边界 reach 私有面。"""
    rt = AgentRuntime()
    a = _agent()
    th = rt._attach_agent(a)
    st = th.status()
    assert st["session_id"] == a.session_id
    assert st["model"] == a.model
    assert set(st) >= {"session_id", "cwd", "session_name", "input_tokens", "output_tokens",
                       "cost_usd", "context_window", "model", "thinking"}


def test_thread_messages_and_state_snapshot():
    """docs/17 #2：messages()/state() 是重绘 / RPC get_state 的视图地基（从 canonical 树派生）。"""
    rt = AgentRuntime()
    a = _agent()
    th = rt._attach_agent(a)
    # 无会话写者租约 → 空快照（不抛）
    assert th.messages() == []
    state = th.state()
    assert state["messages"] == [] and state["is_processing"] is False
    assert state["session_id"] == a.session_id        # state ⊇ status 字段
    assert "model" in state and "cost_usd" in state


def test_runtime_thread_facade_does_not_expose_internal_state_managers():
    rt = AgentRuntime()
    a = Agent(api_key="test", session_id="facade_sid", permission_mode="bypassPermissions")
    th = rt._attach_agent(a)
    assert not hasattr(th, "session_manager")
    assert not hasattr(th, "task_manager")
    assert not hasattr(th, "background_tasks")


def test_readonly_session_view_hides_mutation_methods():
    from nanocode.session.manager import SessionManager
    from nanocode.session import tree as T

    rt = AgentRuntime()
    a = Agent(api_key="test", session_id="readonly_sid", permission_mode="bypassPermissions")
    mgr = SessionManager.create("readonly_sid")
    a._session_mgr = mgr
    user = mgr.append_message(T.user_message("hello"))
    th = rt._attach_agent(a)

    view = th.readonly_session()
    assert view is not None
    assert view.get_leaf() == user.id
    assert not hasattr(view, "append_label")
    th.set_entry_label(user.id, "mark")
    assert mgr.labels()[user.id] == "mark"
    mgr.close()


def test_thread_event_boundary_is_jsonable():
    rt = AgentRuntime()
    a = _agent()
    th = rt._attach_agent(a)
    a.emit(NoticeRaised(text="hello"))
    env = th.events()[-1]
    assert env["event"] == {"text": "hello", "level": "info", "kind": "notice_raised"}
    json.dumps(env)


def test_final_response_derived_from_event_stream():
    """docs/17 Phase 0：TurnResult.final_response 从 agent 的 emit 流（AssistantDelta.text）派生，
    无需 capturing sink。"""
    rt = AgentRuntime()
    a = _agent()

    async def fake_chat(prompt):
        a._emit_block("hello world")

    th = rt._attach_agent(a)
    th._session.run_turn = fake_chat
    res = asyncio.run(th.run("q"))
    assert res.final_response == "hello world"


def test_final_response_resets_between_turns():
    rt = AgentRuntime()
    a = _agent()
    seq = iter(["first", "second"])

    async def fake_chat(prompt):
        a._emit_block(next(seq))

    th = rt._attach_agent(a)
    th._session.run_turn = fake_chat
    r1 = asyncio.run(th.run("a"))
    r2 = asyncio.run(th.run("b"))
    assert r1.final_response == "first"
    assert r2.final_response == "second"   # 不累积（每 turn 入口 reset_final_text）


def test_approval_manager_attaches_both_channels():
    a = _agent()

    async def cf(msg): return True
    async def pf(msg): return {"choice": "execute"}

    mgr = ApprovalManager(confirm_fn=cf, plan_approval_fn=pf)
    AgentRuntime()._attach_agent(a, approvals=mgr)
    assert a.confirm_fn is cf
    assert a._plan_approval_fn is pf


def test_approval_manager_none_leaves_defaults():
    a = _agent()
    before_confirm = a.confirm_fn
    AgentRuntime()._attach_agent(a, approvals=ApprovalManager())  # both None
    assert a.confirm_fn is before_confirm   # 不动默认（None→阻塞 input 回退）


def test_execute_user_shell_emits_audit_events():
    rt = AgentRuntime()
    a = _agent()
    th = rt._attach_agent(a)
    out = asyncio.run(th.execute_user_shell("echo runtime-shell"))
    assert "runtime-shell" in out
    types = [e["type"] for e in th.events()]
    assert "user_shell_started" in types
    assert "user_shell_completed" in types


def test_execute_user_shell_runs_in_runtime_cwd(tmp_path):
    cwd = tmp_path / "shell-cwd"
    cwd.mkdir()
    rt = AgentRuntime()
    cfg = AgentConfig(api_key="test", session_id="shellcwd",
                      permission_mode="bypassPermissions", cwd=str(cwd))
    th = rt.thread_start(cfg)
    try:
        out = asyncio.run(th.execute_user_shell("pwd"))
        assert str(cwd.resolve()) in out
        done = next(e for e in th.events() if e["type"] == "user_shell_completed")
        assert done["event"]["cwd"] == str(cwd.resolve())
    finally:
        th.release_lease()


def test_model_run_shell_receives_runtime_cwd(monkeypatch, tmp_path):
    # docs/19：run_shell 经 SandboxManager；cwd 来自 HostContext（runtime cwd），非 tool input。
    cwd = tmp_path / "tool-cwd"
    cwd.mkdir()
    rt = AgentRuntime()
    cfg = AgentConfig(api_key="test", session_id="toolcwd",
                      permission_mode="bypassPermissions", cwd=str(cwd))
    th = rt.thread_start(cfg)
    captured = {}

    async def fake_exec(request, host, policy, approval):
        captured["cwd"] = str(host.cwd)
        return "ok"

    monkeypatch.setattr(th._agent._sandbox, "execute_shell", fake_exec)
    try:
        out = asyncio.run(th._agent._execute_tool_call("run_shell", {"command": "git status"}))
        assert out == "ok"
        assert captured["cwd"] == str(cwd.resolve())
    finally:
        th.release_lease()


def test_set_session_name_emits_runtime_event():
    from nanocode.session.manager import SessionManager

    rt = AgentRuntime()
    a = Agent(api_key="test", session_id="namesid", permission_mode="bypassPermissions")
    mgr = SessionManager.create("namesid")
    a._session_mgr = mgr
    th = rt._attach_agent(a)
    seen = []
    th.subscribe(seen.append)

    th.set_session_name("runtime name")

    assert mgr.name() == "runtime name"
    assert seen[-1]["type"] == "session_info_changed"
    assert seen[-1]["event"]["name"] == "runtime name"
    mgr.close()
