import pytest

from nanocode.tasks.manager import TaskManager


def test_create_and_get_task():
    m = TaskManager()
    t = m.create_task("shell", "pytest -q")
    assert t.id == "task-001" and t.status == "running" and t.started_at
    assert m.get_task("task-001") is t


def test_create_task_rejects_subagent_kind():
    m = TaskManager()
    with pytest.raises(ValueError, match="unknown host task kind"):
        m.create_task("subagent", "review")


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
    state = m.to_state()
    m2 = TaskManager()
    m2.load_state(state)
    assert m2.get_task("task-001").description == "a"
    assert m2.create_task("shell", "b").id == "task-002"     # 续号，不碰撞


def test_load_state_skips_old_subagent_task_records():
    m = TaskManager()
    m.load_state({
        "task_seq": 7,
        "tasks": [
            {"id": "task-001", "kind": "subagent", "description": "old projection"},
            {"id": "task-002", "kind": "shell", "description": "kept"},
        ],
    })
    assert m.get_task("task-001") is None
    assert m.get_task("task-002").description == "kept"
    assert m.create_task("shell", "next").id == "task-008"
