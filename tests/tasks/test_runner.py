"""后台 shell runner：把 SandboxManager.execute_background 结果落库（docs/19）。

后台命令经唯一规划点 SandboxManager（默认 native OS 沙盒）；本机有 seatbelt 时实跑确认。
"""
import asyncio

from nanocode.tasks.manager import TaskManager
from nanocode.tasks import runner

from .._helpers import sandbox_bg_args


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
    sandbox, request, host, policy, approval = sandbox_bg_args("echo done", tmp_path)
    asyncio.run(runner.run_shell_background_task(m, sandbox, t.id, request, host, policy, approval, out, err))
    rec = m.get_task(t.id)
    assert rec.status == "completed" and rec.exit_code == 0 and rec.ended_at is not None
    assert "done" in rec.result_summary


def test_run_shell_background_task_failed(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "exit 2")
    out = str(tmp_path / "o.log"); err = str(tmp_path / "e.log")
    sandbox, request, host, policy, approval = sandbox_bg_args("exit 2", tmp_path)
    asyncio.run(runner.run_shell_background_task(m, sandbox, t.id, request, host, policy, approval, out, err))
    assert m.get_task(t.id).status == "failed" and m.get_task(t.id).exit_code == 2


def test_run_shell_background_task_cancel_records_terminal(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "sleep 5")
    out = str(tmp_path / "o.log"); err = str(tmp_path / "e.log")
    sandbox, request, host, policy, approval = sandbox_bg_args("sleep 5", tmp_path)

    async def scenario():
        task = asyncio.create_task(
            runner.run_shell_background_task(m, sandbox, t.id, request, host, policy, approval, out, err))
        await asyncio.sleep(0.1); task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
    assert m.get_task(t.id).status == "cancelled"


def test_run_shell_background_blocked_records_blocked(tmp_path):
    # vm profile 后台：microVM 无法异步后台 → blocked（fail-closed，不裸跑）。
    m = TaskManager(); t = m.create_task("shell", "make build")
    out = str(tmp_path / "o.log"); err = str(tmp_path / "e.log")
    sandbox, request, host, policy, approval = sandbox_bg_args("make build", tmp_path, profile="vm")
    asyncio.run(runner.run_shell_background_task(m, sandbox, t.id, request, host, policy, approval, out, err))
    rec = m.get_task(t.id)
    assert rec.status == "blocked"
    assert rec.ended_at is not None
