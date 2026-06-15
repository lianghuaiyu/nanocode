"""G²：argv-wrap 受限后台路由测试。

覆盖：
- build_argv（seatbelt 全平台纯字符串；bwrap monkeypatch _TRUSTED_BWRAP 纯 argv）。
- plan_background 决策（off / 只读 / escalate / seatbelt+后端 / seatbelt+无后端 / auto）。
- run_background 路由（sandbox→exec、host→shell、blocked→不 spawn 且带 blocked 键）。
- tasks/runner 处理 blocked（落库 status="blocked"）。
"""

import asyncio

import pytest

from nanocode.tools import run_shell
from nanocode.tools.sandbox_backends import seatbelt, bwrap
from nanocode.tools.sandbox_backends.base import WORKSPACE_WRITE


# ─── 1. build_argv（纯函数） ─────────────────────────────────────────────


def test_seatbelt_build_argv_shape():
    argv = seatbelt.build_argv("echo hi", cwd="/private/tmp")
    assert argv[0] == seatbelt.SANDBOX_EXEC
    assert argv[1] == "-p"
    profile = argv[2]
    assert "(deny default)" in profile
    assert argv[3:] == ["/bin/sh", "-c", "echo hi"]


def test_seatbelt_build_argv_defaults_cwd(monkeypatch, tmp_path):
    import os

    monkeypatch.chdir(tmp_path)
    argv = seatbelt.build_argv("ls")
    assert argv[0] == seatbelt.SANDBOX_EXEC
    real = os.path.realpath(str(tmp_path))
    assert f'(subpath "{real}")' in argv[2]


def test_bwrap_build_argv_shape(monkeypatch, tmp_path):
    # 钉死可信目录到 tmp_path 下的可执行 bwrap → argv[0] 是该可信绝对路径（不走 PATH）。
    target = tmp_path / "bwrap"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    monkeypatch.setattr(bwrap, "_TRUSTED_BWRAP", (str(target),))
    argv = bwrap.build_argv("echo hi", cwd=str(tmp_path))
    assert argv[0] == str(target)
    assert "--unshare-net" in argv
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_bwrap_build_argv_fail_closed_when_missing(monkeypatch, tmp_path):
    # 可信目录里无 bwrap → fail-closed（raise），绝不裸跑宿主。
    monkeypatch.setattr(bwrap, "_TRUSTED_BWRAP", (str(tmp_path / "nope-bwrap"),))
    with pytest.raises(FileNotFoundError):
        bwrap.build_argv("echo hi", cwd=str(tmp_path))


# ─── 2. plan_background 决策 ─────────────────────────────────────────────


def _set_mode(monkeypatch, mode):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", mode)


def test_plan_off_returns_host(monkeypatch):
    _set_mode(monkeypatch, "off")
    assert run_shell.plan_background({"command": "rm -rf /"}) == ("host", None)


def test_plan_readonly_returns_host(monkeypatch):
    _set_mode(monkeypatch, "seatbelt")
    # ls 在只读白名单内 → classify 为 host
    assert run_shell.plan_background({"command": "ls"}) == ("host", None)


def test_plan_escalate_returns_host(monkeypatch):
    _set_mode(monkeypatch, "seatbelt")
    kind, payload = run_shell.plan_background({"command": "rm -rf x", "escalate": True})
    assert (kind, payload) == ("host", None)


def test_plan_seatbelt_with_backend_returns_sandbox_backend(monkeypatch):
    _set_mode(monkeypatch, "seatbelt")

    class _FakeBackend:
        @staticmethod
        def build_argv(command, *, posture, cwd):
            return ["SENTINEL", posture, command]

    import nanocode.tools.sandbox_backends as sb

    monkeypatch.setattr(sb, "resolve_native_backend", lambda: _FakeBackend)
    # classify_shell_runtime 必须为 sandbox：用非只读命令
    kind, info = run_shell.plan_background({"command": "make build"})
    assert kind == "sandbox"
    # G/round-2：plan_shell 返回的是后端模块（caller 自行 build_argv），不再是预拼 argv。
    assert info is _FakeBackend


def test_plan_seatbelt_no_backend_returns_blocked(monkeypatch):
    _set_mode(monkeypatch, "seatbelt")
    import nanocode.tools.sandbox_backends as sb

    monkeypatch.setattr(sb, "resolve_native_backend", lambda: None)
    kind, reason = run_shell.plan_background({"command": "make build"})
    assert kind == "blocked"
    assert "escalate=true" in reason


def test_plan_auto_returns_blocked(monkeypatch):
    _set_mode(monkeypatch, "auto")
    kind, reason = run_shell.plan_background({"command": "make build"})
    assert kind == "blocked"
    assert "microVM" in reason or "auto" in reason


# ─── 3. run_background 路由 ─────────────────────────────────────────────


def _fake_proc(returncode=0):
    class _P:
        def __init__(self):
            self.returncode = returncode

        async def wait(self):
            return returncode

    return _P()


def test_run_background_sandbox_uses_exec(monkeypatch, tmp_path):
    sentinel_argv = ["/usr/bin/sandbox-exec", "-p", "PROFILE", "/bin/sh", "-c", "echo hi"]

    class _FakeBackend:
        @staticmethod
        def build_argv(command, *, posture, cwd):
            return sentinel_argv

    # run_background 经 plan_shell(context="background")；sandbox 时 info 是后端模块，
    # run_background 自行 build_argv → exec。monkeypatch plan_shell 返回后端。
    monkeypatch.setattr(
        run_shell, "plan_shell", lambda inp, *, context="foreground": ("sandbox", _FakeBackend)
    )
    captured = {}

    async def fake_exec(*argv, stdout=None, stderr=None):
        captured["argv"] = list(argv)
        return _fake_proc(0)

    async def fake_shell(*a, **k):
        captured["shell"] = True
        return _fake_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    out = str(tmp_path / "o.log")
    err = str(tmp_path / "e.log")
    r = asyncio.run(run_shell.run_background({"command": "echo hi"}, stdout_path=out, stderr_path=err))
    assert captured["argv"] == sentinel_argv
    assert "shell" not in captured
    assert r["exit_code"] == 0
    assert "blocked" not in r


def test_run_background_host_uses_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run_shell, "plan_shell", lambda inp, *, context="foreground": ("host", None)
    )
    captured = {}

    async def fake_exec(*a, **k):
        captured["exec"] = True
        return _fake_proc(0)

    async def fake_shell(command, stdout=None, stderr=None, cwd=None):
        captured["command"] = command
        captured["cwd"] = cwd
        return _fake_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    out = str(tmp_path / "o.log")
    err = str(tmp_path / "e.log")
    r = asyncio.run(run_shell.run_background({"command": "echo bg"}, stdout_path=out, stderr_path=err))
    assert captured["command"] == "echo bg"
    assert captured["cwd"] is None
    assert "exec" not in captured
    assert r["exit_code"] == 0


def test_run_background_host_uses_explicit_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run_shell, "plan_shell", lambda inp, *, context="foreground": ("host", None)
    )
    captured = {}

    async def fake_shell(command, stdout=None, stderr=None, cwd=None):
        captured["cwd"] = cwd
        return _fake_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    out = str(tmp_path / "o.log")
    err = str(tmp_path / "e.log")
    workdir = str(tmp_path / "work")
    (tmp_path / "work").mkdir()
    r = asyncio.run(run_shell.run_background({"command": "echo bg", "_cwd": workdir},
                                             stdout_path=out, stderr_path=err))
    assert r["exit_code"] == 0
    assert captured["cwd"] == workdir


def test_run_background_blocked_does_not_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run_shell, "plan_shell",
        lambda inp, *, context="foreground": ("blocked", "no sandbox; add escalate=true"),
    )
    spawned = []

    async def fake_exec(*a, **k):
        spawned.append("exec")
        return _fake_proc(0)

    async def fake_shell(*a, **k):
        spawned.append("shell")
        return _fake_proc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    out = str(tmp_path / "o.log")
    err = str(tmp_path / "e.log")
    r = asyncio.run(run_shell.run_background({"command": "rm -rf x"}, stdout_path=out, stderr_path=err))
    assert r["blocked"] == "no sandbox; add escalate=true"
    assert r["exit_code"] is None
    assert spawned == []  # 未 spawn 任何子进程


# ─── 4. tasks/runner 处理 blocked ───────────────────────────────────────


def test_runner_records_blocked(monkeypatch, tmp_path):
    from nanocode.tasks.manager import TaskManager
    from nanocode.tasks import runner

    async def fake_run_background(inp, *, stdout_path, stderr_path):
        return {
            "exit_code": None,
            "timed_out": False,
            "cancelled": False,
            "error": None,
            "blocked": "background sandbox command refused — add escalate=true",
        }

    monkeypatch.setattr(run_shell, "run_background", fake_run_background)
    m = TaskManager()
    t = m.create_task("shell", "make build")
    out = str(tmp_path / "o.log")
    err = str(tmp_path / "e.log")
    asyncio.run(runner.run_shell_background_task(m, t.id, "make build", out, err))
    rec = m.get_task(t.id)
    assert rec.status == "blocked"
    assert "escalate=true" in rec.result_summary
    assert rec.ended_at is not None  # blocked 是 terminal 状态


# ─── 5. 可选 skipif-darwin 集成：后台跑沙盒命令，写 cwd 外被拒 ──────────────

_smoke = pytest.mark.skipif(
    not seatbelt.is_available(),
    reason="requires macOS with sandbox-exec",
)


@_smoke
def test_smoke_background_sandbox_denies_outside_write(monkeypatch, tmp_path):
    import os
    import sys

    if sys.platform != "darwin":
        pytest.skip("seatbelt smoke is darwin-only")
    from nanocode.tasks.manager import TaskManager
    from nanocode.tasks import runner

    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    monkeypatch.chdir(tmp_path)
    home = os.path.realpath(os.path.expanduser("~"))
    roots = seatbelt._writable_roots(os.path.realpath(str(tmp_path)))
    if any(home == r or home.startswith(r + os.sep) for r in roots):
        pytest.skip("HOME inside writable roots; cannot test denial")
    outside = os.path.join(home, f".nanocode_bg_outside_{os.getpid()}_{tmp_path.name}.txt")
    if os.path.exists(outside):
        os.unlink(outside)
    m = TaskManager()
    t = m.create_task("shell", "bg write")
    out = str(tmp_path / "o.log")
    err = str(tmp_path / "e.log")
    try:
        asyncio.run(
            runner.run_shell_background_task(
                m, t.id, f"echo x > {outside}", out, err
            )
        )
        rec = m.get_task(t.id)
        # 任务完成（沙盒生效，命令非零退出 → failed），且写入被拒。
        assert rec.status in ("failed", "completed")
        assert not os.path.exists(outside)
    finally:
        if os.path.exists(outside):
            os.unlink(outside)
