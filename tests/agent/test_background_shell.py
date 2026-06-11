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
    from nanocode.tools.registry import tool_definitions
    names = {t["name"] for t in tool_definitions}
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


def test_task_tools_default_mode_allow():
    from nanocode.tools import check_permission
    for name in ("task_list", "task_output", "task_stop"):
        perm = check_permission(name, {"task_id": "task-001"}, "default")
        assert perm["action"] == "allow"


def test_inject_finished_tasks_appends_to_last_user():
    a = _agent()
    t = a.task_manager.create_task("shell", "echo hi")
    a.task_manager.update_task(t.id, status="completed", exit_code=0, result_summary="hi")
    msgs = [{"role": "user", "content": "do something"}]
    a._inject_finished_tasks(msgs)
    assert "<system-reminder>" in msgs[-1]["content"] and t.id in msgs[-1]["content"]
    assert a.task_manager.get_task(t.id).injected is True
    before = [dict(m) for m in msgs]
    a._inject_finished_tasks(msgs)
    assert msgs == before


def test_inject_finished_tasks_skips_running():
    a = _agent(); a.task_manager.create_task("shell", "still going")
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_finished_tasks(msgs)
    assert msgs == [{"role": "user", "content": "hi"}]


def test_inject_finished_tasks_new_user_when_last_not_user():
    a = _agent(); t = a.task_manager.create_task("shell", "x")
    a.task_manager.update_task(t.id, status="completed")
    msgs = [{"role": "assistant", "content": "ok"}]
    a._inject_finished_tasks(msgs)
    assert msgs[-1]["role"] == "user" and "<system-reminder>" in msgs[-1]["content"]


def test_inject_finished_tasks_handles_list_content():
    a = _agent(); t = a.task_manager.create_task("shell", "x")
    a.task_manager.update_task(t.id, status="completed")
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    a._inject_finished_tasks(msgs)
    last = msgs[-1]["content"]
    assert isinstance(last, list) and any(
        b.get("type") == "text" and "<system-reminder>" in b.get("text", "") for b in last)
    assert a.task_manager.get_task(t.id).injected is True

