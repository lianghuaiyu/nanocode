"""Task 6: v2 state 接线 + resume 标记 lost。

- restore_session 载入 state.tasks/subagents；running task→lost、completed 不变、
  running subagent→lost、续号不碰撞。
- 无 state 时 noop。
- _persist_state() 写 v2 state.json（tasks 含 description）。
"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.session import v2 as _session_v2
from nanocode.tasks.models import TaskRecord, SubAgentRecord


def _agent(**kw):
    return Agent(api_key="test",
                 permission_mode="bypassPermissions", session_id="rlsid", **kw)


# ─── restore_session 载入 state ────────────────────────────────


def test_restore_loads_tasks_from_state():
    a = _agent()
    state = {
        "tasks": [{"id": "task-001", "kind": "shell", "status": "running",
                   "description": "run tests"}],
        "subagents": [],
        "task_seq": 1, "agent_seq": 0,
    }
    a._reload_task_state(state)
    t = a.task_manager.get_task("task-001")
    assert t is not None
    assert t.description == "run tests"


def test_restore_loads_subagents_from_state():
    a = _agent()
    state = {
        "tasks": [],
        "subagents": [{"id": "agent-001", "type": "coder", "status": "running",
                       "description": "do x", "model": "m", "provider": "anthropic"}],
        "task_seq": 0, "agent_seq": 1,
    }
    a._reload_task_state(state)
    rec = a.task_manager.get_subagent("agent-001")
    assert rec is not None
    assert rec.type == "coder"


def test_restore_marks_running_task_as_lost():
    a = _agent()
    state = {
        "tasks": [{"id": "task-001", "kind": "shell", "status": "running",
                   "description": "lost task"}],
        "subagents": [],
        "task_seq": 1, "agent_seq": 0,
    }
    a._reload_task_state(state)
    t = a.task_manager.get_task("task-001")
    assert t.status == "lost"


def test_restore_preserves_completed_task():
    a = _agent()
    state = {
        "tasks": [{"id": "task-001", "kind": "shell", "status": "completed",
                   "description": "done"}],
        "subagents": [],
        "task_seq": 1, "agent_seq": 0,
    }
    a._reload_task_state(state)
    t = a.task_manager.get_task("task-001")
    assert t.status == "completed"


def test_restore_marks_running_subagent_as_lost():
    a = _agent()
    state = {
        "tasks": [],
        "subagents": [{"id": "agent-001", "type": "coder", "status": "running",
                       "description": "sub"}],
        "task_seq": 0, "agent_seq": 1,
    }
    a._reload_task_state(state)
    rec = a.task_manager.get_subagent("agent-001")
    assert rec.status == "lost"


def test_restore_marks_idle_subagent_as_lost():
    a = _agent()
    state = {
        "tasks": [],
        "subagents": [{"id": "agent-001", "type": "coder", "status": "idle",
                       "description": "sub"}],
        "task_seq": 0, "agent_seq": 1,
    }
    a._reload_task_state(state)
    rec = a.task_manager.get_subagent("agent-001")
    assert rec.status == "lost"


def test_restore_preserves_completed_subagent():
    a = _agent()
    state = {
        "tasks": [],
        "subagents": [{"id": "agent-001", "type": "coder", "status": "completed",
                       "description": "sub"}],
        "task_seq": 0, "agent_seq": 1,
    }
    a._reload_task_state(state)
    rec = a.task_manager.get_subagent("agent-001")
    assert rec.status == "completed"


def test_restore_seq_no_collision():
    """续号不碰撞：restore 后新建 task/subagent 的 id 不重复。"""
    a = _agent()
    state = {
        "tasks": [{"id": "task-003", "kind": "shell", "status": "completed",
                   "description": "old"}],
        "subagents": [{"id": "agent-002", "type": "coder", "status": "completed",
                       "description": "old sub"}],
        "task_seq": 3, "agent_seq": 2,
    }
    a._reload_task_state(state)
    new_t = a.task_manager.create_task("shell", "new")
    new_a = a.task_manager.create_subagent(type="coder", description="new sub")
    assert new_t.id == "task-004"
    assert new_a.id == "agent-003"


def test_restore_noop_when_no_state():
    """无 state 时 noop（docs/14 SessionLease：state 重载由 _reload_task_state 承担）。"""
    a = _agent()
    a._reload_task_state(None)
    assert a.task_manager.list_tasks() == []
    assert a.task_manager.list_subagents() == []


# ─── _persist_state 写 v2 ────────────────────────────────────


def test_persist_state_writes_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_v2, "session_root", lambda sid: tmp_path / sid)
    a = _agent()
    a.task_manager.create_task("shell", "my task")
    a._persist_state()
    state = _session_v2.read_state("rlsid")
    assert state is not None
    assert state["session_id"] == "rlsid"
    assert len(state["tasks"]) == 1
    assert state["tasks"][0]["description"] == "my task"


def test_persist_state_includes_subagents(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_v2, "session_root", lambda sid: tmp_path / sid)
    a = _agent()
    a.task_manager.create_subagent(type="coder", description="sub")
    a._persist_state()
    state = _session_v2.read_state("rlsid")
    assert len(state["subagents"]) == 1
    assert state["subagents"][0]["description"] == "sub"
