from nanocode.tools.run_shell import run_structured, run


def test_structured_success():
    r = run_structured({"command": "echo hi"})
    assert r["exit_code"] == 0 and r["stdout"].strip() == "hi" and r["error"] is None


def test_structured_nonzero():
    r = run_structured({"command": "exit 7"})
    assert r["exit_code"] == 7 and not r["timed_out"]


def test_structured_stdin():
    r = run_structured({"command": "cat", "stdin": "PAYLOAD"})
    assert "PAYLOAD" in r["stdout"]


def test_structured_timeout():
    r = run_structured({"command": "sleep 2", "timeout": 100})
    assert r["timed_out"] is True


def test_run_text_unchanged_success():
    assert run({"command": "echo hi"}).strip() == "hi"


def test_run_text_unchanged_failure():
    out = run({"command": "exit 3"})
    assert "Command failed (exit code 3)" in out


import asyncio
from nanocode.tools import run_shell as rs


def test_schema_has_run_in_background():
    props = rs.SCHEMA["input_schema"]["properties"]
    assert "run_in_background" in props
    assert "run_in_background" not in rs.SCHEMA["input_schema"]["required"]


def test_run_background_writes_logs_and_exit0(tmp_path):
    out = tmp_path / "stdout.log"; err = tmp_path / "stderr.log"
    r = asyncio.run(rs.run_background(
        {"command": "echo hi; echo oops 1>&2"},
        stdout_path=str(out), stderr_path=str(err)))
    assert r["exit_code"] == 0 and r["timed_out"] is False and r["cancelled"] is False
    assert "hi" in out.read_text()
    assert "oops" in err.read_text()


def test_run_background_nonzero(tmp_path):
    out = tmp_path / "o.log"; err = tmp_path / "e.log"
    r = asyncio.run(rs.run_background(
        {"command": "exit 5"}, stdout_path=str(out), stderr_path=str(err)))
    assert r["exit_code"] == 5


def test_run_background_timeout(tmp_path):
    out = tmp_path / "o.log"; err = tmp_path / "e.log"
    r = asyncio.run(rs.run_background(
        {"command": "sleep 2", "timeout": 100},
        stdout_path=str(out), stderr_path=str(err)))
    assert r["timed_out"] is True


def test_run_background_cancelled(tmp_path):
    out = tmp_path / "o.log"; err = tmp_path / "e.log"
    async def scenario():
        task = asyncio.create_task(rs.run_background(
            {"command": "sleep 5"}, stdout_path=str(out), stderr_path=str(err)))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            return "raised"
    assert asyncio.run(scenario()) == "raised"
