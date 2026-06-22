import asyncio
from nanocode.runs.models import AgentRunRecord
from nanocode.tasks.manager import TaskManager
from nanocode.tools import tasks_tool as tt


def test_schemas_present():
    for s in (tt.LIST_SCHEMA, tt.OUTPUT_SCHEMA, tt.STOP_SCHEMA):
        assert "name" in s and "input_schema" in s
    assert tt.LIST_SCHEMA["name"] == "task_list"
    assert tt.OUTPUT_SCHEMA["name"] == "task_output"
    assert tt.STOP_SCHEMA["name"] == "task_stop"
    assert "task_id" in tt.STOP_SCHEMA["input_schema"]["required"]


def test_list_tasks_text_filters():
    m = TaskManager(); m.create_task("shell", "a"); t = m.create_task("shell", "b")
    m.update_task(t.id, status="completed")
    assert "task-001" in tt.list_tasks_text(m, None, None)
    assert "task-002" not in tt.list_tasks_text(m, "running", None)
    assert "no" in tt.list_tasks_text(TaskManager(), None, None).lower()


def test_task_output_text(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "echo hi")
    out = tmp_path / "stdout.log"; err = tmp_path / "stderr.log"
    out.write_text("HELLO_STDOUT"); err.write_text("HELLO_STDERR")
    m.update_task(t.id, status="completed", exit_code=0, stdout_path=str(out),
                  stderr_path=str(err), result_summary="done")
    txt = tt.task_output_text(m, t.id, tail_bytes=8000)
    assert "completed" in txt and "HELLO_STDOUT" in txt and "HELLO_STDERR" in txt and str(out) in txt


def test_task_output_truncates(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "x")
    out = tmp_path / "o.log"; out.write_text("A" * 5000)
    m.update_task(t.id, status="completed", stdout_path=str(out))
    txt = tt.task_output_text(m, t.id, tail_bytes=100)
    assert "A" * 100 in txt and "A" * 200 not in txt


def test_task_output_unknown():
    assert "unknown" in tt.task_output_text(TaskManager(), "task-999", 10).lower()


def test_task_output_text_prints_result_path_for_host_job(tmp_path):
    m = TaskManager(); t = m.create_task("memory_consolidate", "scan memories")
    rp = tmp_path / "result.md"; rp.write_text("the full result body")
    m.update_task(t.id, status="completed", result_path=str(rp),
                  result_summary="scanned memories")
    txt = tt.task_output_text(m, t.id, tail_bytes=8000)
    assert str(rp) in txt
    assert "scanned memories" in txt


def test_task_stop_unknown():
    assert "unknown" in asyncio.run(tt.task_stop(TaskManager(), set(), "task-999")).lower()


def test_task_stop_already_terminal():
    m = TaskManager(); t = m.create_task("shell", "x"); m.update_task(t.id, status="completed")
    res = asyncio.run(tt.task_stop(m, set(), t.id))
    assert "already" in res.lower() or "terminal" in res.lower()


def test_task_stop_cancels_running(tmp_path):
    from nanocode.tasks import runner
    from .._helpers import sandbox_bg_args
    m = TaskManager(); t = m.create_task("shell", "sleep 5")
    out = str(tmp_path / "o.log"); err = str(tmp_path / "e.log")
    m.update_task(t.id, stdout_path=out, stderr_path=err)
    sandbox, request, host, policy, approval = sandbox_bg_args("sleep 5", tmp_path)
    async def scenario():
        bg = set()
        task = asyncio.create_task(
            runner.run_shell_background_task(m, sandbox, t.id, request, host, policy, approval, out, err))
        task._nanocode_task_id = t.id; bg.add(task)
        await asyncio.sleep(0.1)
        return await tt.task_stop(m, bg, t.id)
    res = asyncio.run(scenario())
    assert "stop" in res.lower() or "cancel" in res.lower()
    assert m.get_task(t.id).status == "cancelled"


# ─── Stage D: subagent renderers ────────────────────────────

def _run_record(**kw):
    data = {
        "run_id": "sess-child",
        "child_session_id": "sess-child",
        "parent_session_id": "sess-parent",
        "status": "completed",
        "agent_type": "coder",
        "description": "inspect parser",
        "model": {"provider": "anthropic", "modelId": "claude-opus-4-6"},
        "background": False,
        "context_mode": "fresh",
        "isolation": "shared",
        "summary": "inspect parser",
    }
    data.update(kw)
    return AgentRunRecord(**data)


def test_list_subagents_text_empty():
    assert "no sub-agent" in tt.list_subagents_text([]).lower()


def test_list_subagents_text_lists():
    txt = tt.list_subagents_text([
        _run_record(child_session_id="sess-a", run_id="sess-a"),
        _run_record(child_session_id="sess-b", run_id="sess-b",
                    agent_type="explore", summary="scan repo"),
    ])
    assert "sess-a" in txt and "sess-b" in txt
    assert "coder" in txt and "scan repo" in txt


def test_subagent_detail_text():
    txt = tt.subagent_detail_text(_run_record())
    assert "sess-child" in txt and "coder" in txt
    assert "anthropic" in txt and "claude-opus-4-6" in txt


def test_subagent_detail_text_unknown():
    assert "unknown" in tt.subagent_detail_text(None).lower()


def test_subagent_detail_text_surfaces_run_record_paths():
    txt = tt.subagent_detail_text(_run_record(
        result_path="/tmp/result.md",
        worktree_path="/tmp/worktree",
        error="boom",
    ))
    assert "Result: /tmp/result.md" in txt
    assert "Worktree: /tmp/worktree" in txt
    assert "Error: boom" in txt
