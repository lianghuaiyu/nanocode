import asyncio
from nanocode.tasks.manager import TaskManager
from nanocode.tasks import runner


def test_tail_file(tmp_path):
    p = tmp_path / "log.txt"; p.write_text("0123456789abcdef")
    assert runner.tail_file(str(p), 4) == "cdef"
    assert runner.tail_file(str(p), 1000) == "0123456789abcdef"
    assert runner.tail_file(str(tmp_path / "missing"), 10) == ""


def test_classify_exit():
    assert runner.classify_exit(0, False, False, None) == "completed"
    assert runner.classify_exit(1, False, False, None) == "failed"
    assert runner.classify_exit(None, True, False, None) == "timed_out"
    assert runner.classify_exit(None, False, True, None) == "cancelled"
    assert runner.classify_exit(None, False, False, "boom") == "failed"


def test_run_shell_background_task_completed(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "echo done")
    out = str(tmp_path / "stdout.log"); err = str(tmp_path / "stderr.log")
    asyncio.run(runner.run_shell_background_task(m, t.id, "echo done", out, err))
    rec = m.get_task(t.id)
    assert rec.status == "completed" and rec.exit_code == 0 and rec.ended_at is not None
    assert "done" in rec.result_summary


def test_run_shell_background_task_failed(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "exit 2")
    out = str(tmp_path / "o.log"); err = str(tmp_path / "e.log")
    asyncio.run(runner.run_shell_background_task(m, t.id, "exit 2", out, err))
    assert m.get_task(t.id).status == "failed" and m.get_task(t.id).exit_code == 2


def test_run_shell_background_task_cancel_records_terminal(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "sleep 5")
    out = str(tmp_path / "o.log"); err = str(tmp_path / "e.log")
    async def scenario():
        task = asyncio.create_task(runner.run_shell_background_task(m, t.id, "sleep 5", out, err))
        await asyncio.sleep(0.1); task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
    assert m.get_task(t.id).status == "cancelled"
