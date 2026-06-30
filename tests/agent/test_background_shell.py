import asyncio
from nanocode.agent.engine import Agent


def _agent():
    return Agent(api_key="test", permission_mode="bypassPermissions")


def test_agent_has_task_manager():
    a = _agent()
    assert a.task_manager is not None and a.task_manager.list_tasks() == []


def test_subagent_shares_parent_task_manager():
    parent = _agent()
    child = Agent(api_key="test", is_sub_agent=True,
                  task_manager=parent.task_manager)
    assert child.task_manager is parent.task_manager


def test_spawn_background_shell_returns_task_id_and_completes(tmp_path):
    a = _agent()
    async def scenario():
        tid = await a._spawn_background_shell("echo hello", timeout_ms=None)
        assert tid == "task-001"
        assert a.task_manager.get_task(tid).status == "running"
        for _ in range(100):
            if a.task_manager.get_task(tid).status != "running":
                break
            await asyncio.sleep(0.02)
        return a.task_manager.get_task(tid)
    rec = asyncio.run(scenario())
    assert rec.status == "completed" and rec.exit_code == 0


def test_spawn_background_shell_tags_main_session_owner():
    a = Agent(api_key="test", permission_mode="bypassPermissions", session_id="main-session")

    async def scenario():
        tid = await a._spawn_background_shell("echo owner", timeout_ms=None)
        for _ in range(100):
            if a.task_manager.get_task(tid).status != "running":
                break
            await asyncio.sleep(0.02)
        return a.task_manager.get_task(tid)

    rec = asyncio.run(scenario())
    assert rec.owner_agent_id == "main-session"


def test_spawn_background_shell_tags_subagent_tree_session_owner():
    parent = _agent()
    child = Agent(
        api_key="test",
        is_sub_agent=True,
        task_manager=parent.task_manager,
        session_id=parent.session_id,
        permission_mode="bypassPermissions",
    )
    child._tree_session_id = "sess_child"

    async def scenario():
        tid = await child._spawn_background_shell("echo child", timeout_ms=None)
        for _ in range(100):
            if parent.task_manager.get_task(tid).status != "running":
                break
            await asyncio.sleep(0.02)
        return parent.task_manager.get_task(tid)

    rec = asyncio.run(scenario())
    assert rec.owner_agent_id == "sess_child"


def test_execute_tool_call_routes_background(tmp_path):
    a = _agent()
    res = asyncio.run(a._execute_tool_call("run_shell", {"command": "echo bg", "run_in_background": True}))
    assert "task-001" in res and "background" in res.lower()
    assert a.task_manager.get_task("task-001") is not None


def test_execute_tool_call_foreground_unchanged():
    a = _agent()
    res = asyncio.run(a._execute_tool_call("run_shell", {"command": "echo fg"}))
    assert "fg" in res and a.task_manager.list_tasks() == []


def test_task_tools_registered():
    from nanocode.tools import REGISTRY
    names = set(REGISTRY.names())
    assert {"task_list", "task_output", "task_stop"} <= names


def test_execute_tool_call_task_list():
    a = _agent()
    async def scenario():
        await a._spawn_background_shell("echo x", None)
        return await a._execute_tool_call("task_list", {})
    assert "task-001" in asyncio.run(scenario())


def test_execute_tool_call_task_output_unknown():
    a = _agent()
    res = asyncio.run(a._execute_tool_call("task_output", {"task_id": "task-404"}))
    assert "unknown" in res.lower()


def test_stop_task_persists_cancelled_state():
    from nanocode.session import v2
    a = Agent(api_key="test", permission_mode="bypassPermissions", session_id="stop_persist")
    t = a.task_manager.create_task("shell", "orphan")
    a.task_manager.update_task(t.id, status="running")

    res = asyncio.run(a.stop_task(t.id))

    state = v2.read_state("stop_persist")
    assert "marked cancelled" in res.lower()
    assert state["tasks"][0]["id"] == t.id
    assert state["tasks"][0]["status"] == "cancelled"


def test_task_tools_default_mode_allow():
    from nanocode.tools import check_permission
    for name in ("task_list", "task_output", "task_stop"):
        perm = check_permission(name, {"task_id": "task-001"}, "default")
        assert perm["action"] == "allow"


def _ftask_custom_msgs(a):
    from nanocode.session import tree as T
    return [e.data.get("content", "") for e in a._session_mgr.entries()
            if e.type == T.CUSTOM_MESSAGE and e.data.get("customType") == "finished_tasks"]


def test_inject_finished_tasks_writes_custom_message_and_dedups():
    from nanocode.session.manager import SessionManager
    a = _agent()
    a._session_mgr = SessionManager.create("bgsh_inj")
    t = a.task_manager.create_task("shell", "echo hi")
    a.task_manager.update_task(t.id, status="completed", exit_code=0, result_summary="hi")
    a.agent_session.inject_finished_tasks()
    (cm,) = _ftask_custom_msgs(a)
    assert "<system-reminder>" in cm and t.id in cm
    assert a.task_manager.get_task(t.id).injected is True
    a.agent_session.inject_finished_tasks()                                  # dedup：不再注入
    assert len(_ftask_custom_msgs(a)) == 1


def test_inject_finished_tasks_skips_running():
    from nanocode.session.manager import SessionManager
    a = _agent(); a.task_manager.create_task("shell", "still going")
    a._session_mgr = SessionManager.create("bgsh_run")
    a.agent_session.inject_finished_tasks()
    assert _ftask_custom_msgs(a) == []


def test_inject_finished_tasks_tree_write_failure_retries(monkeypatch):
    # docs/16 #1：flat 兜底已删——树写失败 → 不标 injected，下一轮重试（不静默丢提醒）。
    from nanocode.session.manager import SessionManager
    a = _agent(); t = a.task_manager.create_task("shell", "x")
    a.task_manager.update_task(t.id, status="completed")
    a._session_mgr = SessionManager.create("bgsh_fail")
    monkeypatch.setattr(a.agent_session, "_tree_custom_message", lambda *args, **kw: False)
    a.agent_session.inject_finished_tasks()
    assert a.task_manager.get_task(t.id).injected is False      # 未推进 dedup → 重试
