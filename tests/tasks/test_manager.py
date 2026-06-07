from nanocode.tasks.manager import TaskManager


def test_create_and_get_task():
    m = TaskManager()
    t = m.create_task("shell", "pytest -q")
    assert t.id == "task-001" and t.status == "running" and t.started_at
    assert m.get_task("task-001") is t
    t2 = m.create_task("subagent", "review", owner_agent_id="agent-001")
    assert t2.id == "task-002" and t2.owner_agent_id == "agent-001"


def test_create_subagent_ids():
    m = TaskManager()
    a = m.create_subagent("coder", "inspect")
    assert a.id == "agent-001" and a.status == "idle"


def test_list_filter_by_status():
    m = TaskManager()
    m.create_task("shell", "a")
    t = m.create_task("shell", "b")
    m.update_task(t.id, status="completed", result_summary="ok")
    assert {x.id for x in m.list_tasks(status="running")} == {"task-001"}
    assert {x.id for x in m.list_tasks(status="completed")} == {"task-002"}
    assert len(m.list_tasks()) == 2


def test_update_sets_ended_at_on_terminal():
    m = TaskManager()
    t = m.create_task("shell", "a")
    m.update_task(t.id, status="completed")
    assert m.get_task(t.id).ended_at is not None


def test_state_roundtrip_continues_ids():
    m = TaskManager()
    m.create_task("shell", "a")
    m.create_subagent("coder", "x")
    state = m.to_state()
    m2 = TaskManager()
    m2.load_state(state)
    assert m2.get_task("task-001").description == "a"
    assert m2.create_task("shell", "b").id == "task-002"     # 续号，不碰撞
    assert m2.create_subagent("coder", "y").id == "agent-002"
