import asyncio
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


def test_task_output_text_prints_result_path_for_subagent(tmp_path):
    """P3: sub-agent task surfaces result.md path (full transcript + findings)."""
    m = TaskManager(); t = m.create_task("subagent", "scan repo")
    rp = tmp_path / "result.md"; rp.write_text("the full result body")
    m.update_task(t.id, status="completed", result_path=str(rp),
                  result_summary="scanned 12 files")
    txt = tt.task_output_text(m, t.id, tail_bytes=8000)
    assert "completed" in txt
    assert "scanned 12 files" in txt
    assert str(rp) in txt


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

def test_list_subagents_text_empty():
    assert "no sub-agent" in tt.list_subagents_text(TaskManager()).lower()


def test_list_subagents_text_lists():
    m = TaskManager()
    m.create_subagent("coder", "inspect parser")
    m.create_subagent("explore", "scan repo")
    txt = tt.list_subagents_text(m)
    assert "agent-001" in txt and "agent-002" in txt
    assert "coder" in txt and "inspect parser" in txt


def test_subagent_detail_text():
    m = TaskManager()
    a = m.create_subagent("coder", "inspect parser", model="claude-opus-4-6", provider="anthropic")
    txt = tt.subagent_detail_text(m, a.id)
    assert "agent-001" in txt and "coder" in txt and "inspect parser" in txt
    assert "anthropic" in txt and "claude-opus-4-6" in txt


def test_subagent_detail_text_unknown():
    assert "unknown" in tt.subagent_detail_text(TaskManager(), "agent-999").lower()


def test_subagent_detail_text_surfaces_artifact_paths():
    # docs/14 Milestone B：wire.jsonl 已退役——artifact 只剩 Result/Meta/Prompt（无 Wire 行）。
    from nanocode.session import v2
    m = TaskManager()
    a = m.create_subagent("coder", "d", model="claude-opus-4-6", provider="anthropic")
    # no artifacts yet -> no Result line even with session_id
    txt = tt.subagent_detail_text(m, a.id, "detsid")
    assert "Result:" not in txt and "Wire:" not in txt
    # write artifacts, then they should be surfaced
    v2.write_agent_result("detsid", a.id, "done")
    v2.write_agent_prompt("detsid", a.id, "task")
    txt2 = tt.subagent_detail_text(m, a.id, "detsid")
    assert "Result:" in txt2 and "result.md" in txt2
    assert "Prompt:" in txt2
    assert "Wire:" not in txt2                          # wire 已退役，不再 surface
    # artifact surfacing is session-bound; without a session id there are no artifact lines
    assert "Result:" not in tt.subagent_detail_text(m, a.id)
