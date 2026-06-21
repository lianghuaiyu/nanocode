"""session/v2 stores host task state; subagent state lives under child run records."""

from nanocode.session import v2
from nanocode.subagents import run_record


def test_task_dir_path_shape():
    d = v2.task_dir("sA", "task-001")
    assert d.is_dir()
    assert d.name == "task-001"
    assert d.parent.name == "tasks"
    assert d.parent.parent.name == "sA"


def test_write_and_read_state():
    v2.write_state("sB", {"session_id": "sB", "tasks": []})
    assert v2.read_state("sB") == {"session_id": "sB", "tasks": []}


def test_subagent_run_record_path_is_child_owned():
    run_record.write_prompt("sess_child", "do the thing")
    run_record.write_result("sess_child", "done")
    assert run_record.prompt_path("sess_child").parts[-2:] == ("subagent-run", "prompt.md")
    assert run_record.result_path("sess_child").read_text(encoding="utf-8") == "done"
