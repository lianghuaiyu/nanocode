from nanocode.session import v2


def test_state_roundtrip():
    assert v2.read_state("s1") is None
    assert v2.is_v2_session("s1") is False
    v2.write_state("s1", {"startTime": "2026-06-06T00:00:00Z", "tasks": []})
    assert v2.is_v2_session("s1") is True
    assert v2.read_state("s1")["startTime"] == "2026-06-06T00:00:00Z"


def test_task_dir_created_under_session():
    d = v2.task_dir("s4", "task-001")
    assert d.is_dir() and d.name == "task-001" and "s4" in str(d)
