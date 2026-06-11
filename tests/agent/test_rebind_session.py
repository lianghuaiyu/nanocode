"""docs/14 P2：Agent.rebind_session —— 原地 finalize 旧 session 状态 + rebuild 新 session 状态。

锁定不变量：session_id/env/tracer/_session_mgr/task_manager 全部重指；计数与 working set 复位；
旧 wire 被 finalize（session_end）；消息从新树重载。drift guard 守 _reset_working_sets 不漏字段。
"""

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager
from nanocode.session import v2 as _session_v2


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


# 受 rebind 复位的 session 维度字段（drift guard 比对 fresh-vs-rebound）
def _working_set(a) -> dict:
    return {
        "_sent_skill_names": a._sent_skill_names,
        "_pending_skill_bodies": a._pending_skill_bodies,
        "_activated_path_skills": a._activated_path_skills,
        "_active_hooks": a._active_hooks,
        "_confirmed_paths": a._confirmed_paths,
        "_read_file_state": a._read_file_state,
        "_files_read": a._files_read,
        "_files_modified": a._files_modified,
        "_already_surfaced_memories": a._already_surfaced_memories,
        "_session_memory_bytes": a._session_memory_bytes,
        "_pre_plan_mode": a._pre_plan_mode,
        "_context_cleared": a._context_cleared,
        "permission_mode": a.permission_mode,
        "_plan_file_path": a._plan_file_path,
        "total_input_tokens": a.total_input_tokens,
        "total_output_tokens": a.total_output_tokens,
        "last_input_token_count": a.last_input_token_count,
        "current_turns": a.current_turns,
    }


def test_rebind_swaps_all_session_keyed_state():
    a = _agent("OLD")
    a._session_mgr = SessionManager.create("OLD")
    a._session_mgr.append_message(T.user_message("old-conversation"))
    old_tracer = a.tracer
    old_tm = a.task_manager
    old_wire = _session_v2.agent_wire_path("OLD", "main")
    # 脏化 working set + 计数，证明 rebind 复位
    a._confirmed_paths.add("/secret"); a._sent_skill_names.add("foo")
    a._files_read.add("/x"); a._read_file_state["/x"] = 1.0
    a._already_surfaced_memories.add("m"); a._session_memory_bytes = 999
    a._active_hooks.append({"k": 1}); a._pre_plan_mode = "default"; a._context_cleared = True
    a.total_input_tokens = 100; a.total_output_tokens = 50
    a.last_input_token_count = 80; a.current_turns = 3

    # 目标 session 有 canonical 树
    SessionManager.create("NEW").append_message(T.user_message("new-conversation"))
    a.rebind_session("NEW")

    import os
    assert a.session_id == "NEW"
    assert os.environ["NANOCODE_SESSION_ID"] == "NEW"
    assert a._session_mgr.session_id == "NEW"
    assert a.tracer is not old_tracer and a.tracer.session_id == "NEW"
    assert a.task_manager is not old_tm                      # fresh task_manager
    # 计数 + working set 全复位（permission_mode 复位到 baseline、非 falsy）
    for k, v in _working_set(a).items():
        if k == "permission_mode":
            assert v == "bypassPermissions"
            continue
        assert not v, f"{k} not reset: {v!r}"
    # 消息从新树重载
    live = str(a._anthropic_messages)
    assert "new-conversation" in live and "old-conversation" not in live
    # 旧 session 被 finalize：旧 wire 写了 session_end，且旧树未被破坏
    assert old_wire.exists() and "session_end" in old_wire.read_text()
    assert any(e.type == T.MESSAGE for e in SessionManager.open("OLD").entries())


def test_rebind_to_same_session_is_noop():
    a = _agent("SAME")
    a._session_mgr = SessionManager.create("SAME")
    old_tracer = a.tracer
    a.total_input_tokens = 42
    a.rebind_session("SAME")
    assert a.tracer is old_tracer                            # 未 finalize/重建
    assert a.total_input_tokens == 42                        # 未复位


def test_rebind_rejected_for_sub_agent():
    a = _agent("SUB")
    a.is_sub_agent = True
    import pytest
    with pytest.raises(RuntimeError):
        a.rebind_session("OTHER")


def test_rebind_drift_guard_matches_fresh_agent():
    # fresh agent（直接构造到空 session）vs rebound agent（OLD→空 NEW2）：working set 必须逐字段相等。
    # 若 _reset_working_sets 漏了某个 __init__ 设的 session 字段，本测试炸——守复位清单不漂移。
    fresh = _agent("FRESH")
    reb = _agent("OLD2")
    reb._session_mgr = SessionManager.create("OLD2")
    # 脏化后 rebind 到一个全新空 session
    reb._confirmed_paths.add("/a"); reb.total_input_tokens = 5; reb._files_read.add("/b")
    reb.rebind_session("NEW2")
    assert _working_set(reb) == _working_set(fresh)
    assert reb._system_prompt == reb._base_system_prompt   # 非 plan → 不带 plan 提示


def test_rebind_resets_plan_mode_to_new_session(monkeypatch):
    # 启动即 --plan 的 agent（baseline=plan）：rebind 后新 session 仍 plan，但 plan 文件/提示重指新 sid，
    # 旧 sid 路径不泄漏（修复 P2 review 的 HIGH：plan prompt/path 跨会话泄漏）。
    a = Agent(api_key="test", trace_enabled=False, session_id="POLD", permission_mode="plan")
    a._session_mgr = SessionManager.create("POLD")
    old_plan_path = a._plan_file_path
    assert old_plan_path and "POLD" in old_plan_path and "POLD" in a._system_prompt
    a.rebind_session("PNEW")
    assert a.permission_mode == "plan"                       # baseline=plan → 新 session 仍 plan
    assert a._plan_file_path and "PNEW" in a._plan_file_path and a._plan_file_path != old_plan_path
    assert "POLD" not in a._system_prompt                    # 旧 sid plan 路径不泄漏进新 prompt
    assert a._pre_plan_mode is None


def test_rebind_from_toggled_plan_reverts_to_base_mode():
    # 运行中 toggle 进 plan（baseline=default）：rebind 后回到 default、_system_prompt 复位为 base。
    a = Agent(api_key="test", trace_enabled=False, session_id="TOG", permission_mode="default")
    a._session_mgr = SessionManager.create("TOG")
    a.toggle_plan_mode()
    assert a.permission_mode == "plan"
    a.rebind_session("TOG2")
    assert a.permission_mode == "default"                    # 回到 baseline，不残留 plan
    assert a._plan_file_path is None
    assert a._system_prompt == a._base_system_prompt


def test_rebind_atomic_on_unreadable_new_session(monkeypatch):
    # codex B1：新 session open 抛错时，rebind 必须在 finalize 旧 session **之前**失败，旧 session 不动。
    import pytest
    from nanocode.session.manager import SessionManager as SM
    a = _agent("ATOM")
    a._session_mgr = SessionManager.create("ATOM")
    a._session_mgr.append_message(T.user_message("keepme"))
    SessionManager.create("BADNEW")                          # exists → True，使 rebind 走 open 分支
    old_tracer = a.tracer

    def boom(sid, **kw):
        raise T.SessionTreeError("corrupt")

    monkeypatch.setattr(SM, "open", boom)
    with pytest.raises(T.SessionTreeError):
        a.rebind_session("BADNEW")
    # 旧 session 原封不动（pre-flight 在 finalize 前抛）
    assert a.session_id == "ATOM"
    assert a.tracer is old_tracer
    assert a._session_mgr.session_id == "ATOM"
