from nanocode.tasks.models import TaskRecord, TASK_KINDS, TERMINAL_TASK_STATUSES


def test_task_record_roundtrip():
    t = TaskRecord(id="task-001", kind="shell", description="pytest -q")
    assert t.status == "running" and t.injected is False
    d = t.to_dict()
    assert d["id"] == "task-001" and d["kind"] == "shell"
    assert TaskRecord.from_dict(d) == t


def test_constants():
    assert "subagent" not in TASK_KINDS and "shell" in TASK_KINDS
    assert "completed" in TERMINAL_TASK_STATUSES and "running" not in TERMINAL_TASK_STATUSES


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
