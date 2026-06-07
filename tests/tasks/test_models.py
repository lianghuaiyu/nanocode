from nanocode.tasks.models import (
    TaskRecord, SubAgentRecord,
    TASK_KINDS, TERMINAL_TASK_STATUSES, SUBAGENT_STATUSES,
)


def test_task_record_roundtrip():
    t = TaskRecord(id="task-001", kind="shell", description="pytest -q")
    assert t.status == "running" and t.injected is False
    d = t.to_dict()
    assert d["id"] == "task-001" and d["kind"] == "shell"
    assert TaskRecord.from_dict(d) == t


def test_subagent_record_defaults_and_roundtrip():
    a = SubAgentRecord(id="agent-001", type="coder", description="inspect parser")
    assert a.status == "idle" and a.task_id is None
    assert SubAgentRecord.from_dict(a.to_dict()) == a


def test_constants():
    assert "subagent" in TASK_KINDS and "shell" in TASK_KINDS
    assert "completed" in TERMINAL_TASK_STATUSES and "running" not in TERMINAL_TASK_STATUSES
    assert "idle" in SUBAGENT_STATUSES


def test_from_dict_ignores_unknown_keys():
    t = TaskRecord.from_dict({"id": "task-009", "kind": "shell", "bogus": 1})
    assert t.id == "task-009" and t.kind == "shell"


def test_task_record_has_exit_code_default_none():
    t = TaskRecord(id="task-001", kind="shell")
    assert t.exit_code is None
    t2 = TaskRecord(id="task-002", kind="shell", exit_code=0)
    assert t2.to_dict()["exit_code"] == 0
    assert TaskRecord.from_dict(t2.to_dict()) == t2


def test_task_kinds_includes_memory_eval_and_optimize():
    from nanocode.tasks.models import TASK_KINDS
    assert "memory_eval" in TASK_KINDS
    assert "memory_optimize" in TASK_KINDS  # 回归保护：A 已加 optimize
