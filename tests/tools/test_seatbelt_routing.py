"""PR-2 路由测试：NANOCODE_SHELL_SANDBOX=seatbelt 把 sandbox 归类的 run_shell 走原生
Seatbelt 后端；off/auto 零回归。A 组用假后端验证分流，B 组 skipif-darwin 真跑 sandbox-exec。"""

import asyncio
import os
import sys

import pytest

from nanocode.tools import permissions, execute, run_shell, sandbox_shell
from nanocode.tools.sandbox_backends import seatbelt


def _run(coro):
    return asyncio.run(coro)


def _set_backend(monkeypatch, backend):
    """round-2：路由经 run_shell.plan_shell → sandbox_backends.resolve_native_backend
    选后端（不再有 execute._native_backend）。在 sandbox_backends 包上 monkeypatch 解析器。"""
    import nanocode.tools.sandbox_backends as sb
    monkeypatch.setattr(sb, "resolve_native_backend", lambda: backend)


class _FakeBackend:
    """记录 run_structured 调用的假原生后端（PR-3 起路由改用 run_structured）。"""

    def __init__(self, result=None):
        # 默认成功结构化结果；测试可传入自定义 dict。
        self.result = result if result is not None else {
            "exit_code": 0, "stdout": "NATIVE_OK", "stderr": "", "timed_out": False, "error": None,
        }
        self.calls = []

    def run_structured(self, inp, *, posture="workspace-write", cwd=None):
        self.calls.append({"inp": inp, "posture": posture, "cwd": cwd})
        return self.result


@pytest.fixture(autouse=True)
def _reset_sandbox_env(monkeypatch):
    """每个测试前清掉 flag，避免外部环境串味（H① 起不再有 _warned_unconfined 标志）。"""
    monkeypatch.delenv("NANOCODE_SHELL_SANDBOX", raising=False)
    yield


# 1. classify 在 seatbelt 档仍生效（C：危险命令现归类 sandbox，不再 host）
def test_classify_in_seatbelt_mode(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    assert permissions.classify_shell_runtime("python x.py") == "sandbox"
    assert permissions.classify_shell_runtime("git status") == "host"
    assert permissions.classify_shell_runtime("rm foo") == "sandbox"


# 2. 路由到 seatbelt：假后端被调用、sandbox_shell 未调用、结果为 NATIVE_OK
def test_routes_to_seatbelt(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    fake = _FakeBackend()
    _set_backend(monkeypatch, fake)
    sbx_calls = []
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python -c 'print(1)'"}))
    assert out == "NATIVE_OK"
    assert len(fake.calls) == 1
    assert fake.calls[0]["inp"]["command"] == "python -c 'print(1)'"
    assert fake.calls[0]["posture"] == "workspace-write"
    assert fake.calls[0]["cwd"] == os.getcwd()
    assert sbx_calls == []


# 3. 后端不可用 fail-closed（H①）：不静默宿主裸跑，返回 escalate 提示，宿主未调用
def test_backend_unavailable_fails_closed(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    _set_backend(monkeypatch, None)
    host_calls = []
    sbx_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert "native OS sandbox unavailable" in out
    assert "escalate=true" in out
    assert host_calls == []  # 不静默回退宿主
    assert sbx_calls == []


# 4. 只读走宿主、危险走沙盒（seatbelt 档；C：危险命令归类 sandbox → 受限）
def test_readonly_uses_host_dangerous_uses_sandbox(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    fake = _FakeBackend()
    _set_backend(monkeypatch, fake)
    host_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    # 只读：走宿主，后端未调用
    out = _run(execute.execute_tool("run_shell", {"command": "git status"}))
    assert out == "HOST_OK"
    assert len(host_calls) == 1
    assert fake.calls == []
    # 危险：进沙盒（受限），宿主未再被调用
    host_calls.clear()
    out = _run(execute.execute_tool("run_shell", {"command": "rm foo"}))
    assert out == "NATIVE_OK"
    assert host_calls == []
    assert len(fake.calls) == 1


# 5. off 档零回归：未设 flag，python x.py → 宿主，后端/microVM 均未调用
def test_off_mode_zero_regression(monkeypatch):
    monkeypatch.delenv("NANOCODE_SHELL_SANDBOX", raising=False)
    fake = _FakeBackend()
    _set_backend(monkeypatch, fake)
    sbx_calls = []
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    monkeypatch.setattr(run_shell, "run", lambda inp: "HOST_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert out == "HOST_OK"
    assert fake.calls == []
    assert sbx_calls == []


# ─── PR-3. 三类语义（机制失败 / 命令失败 / 成功 / 超时）+ 一次性提示 + bypass confined ──

def _seatbelt_fake(monkeypatch, structured):
    """装一个 seatbelt 档 + 返回指定 structured 结果的假后端，返回 (fake, host_calls)。"""
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    fake = _FakeBackend(structured)
    _set_backend(monkeypatch, fake)
    host_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    return fake, host_calls


# 1. 机制失败：run_structured 返回 error → escalate 提示，不调用宿主
def test_machinery_failure_returns_escalate_notice(monkeypatch):
    fake, host_calls = _seatbelt_fake(
        monkeypatch,
        {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "error": "boom"},
    )
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert "native OS sandbox failed" in out
    assert "boom" in out
    assert "escalate=true" in out
    assert len(fake.calls) == 1
    assert host_calls == []  # 不静默回退宿主


# 2. 命令失败（exit≠0）：_NATIVE_FAIL_HINT + "Command failed (exit code 1)"
def test_command_failure_prefixes_native_hint(monkeypatch):
    fake, host_calls = _seatbelt_fake(
        monkeypatch,
        {"exit_code": 1, "stdout": "", "stderr": "nope", "timed_out": False, "error": None},
    )
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert execute._NATIVE_FAIL_HINT in out
    assert "OS sandbox" in out
    assert "escalate=true" in out
    assert "Command failed (exit code 1)" in out
    assert "Stderr: nope" in out
    # 用的是原生提示，而非 microVM 提示
    assert execute._SANDBOX_FAIL_HINT not in out
    assert host_calls == []


# 3. 成功：原样返回 stdout，无任何提示
def test_success_returns_stdout_no_hint(monkeypatch):
    fake, host_calls = _seatbelt_fake(
        monkeypatch,
        {"exit_code": 0, "stdout": "ok\n", "stderr": "", "timed_out": False, "error": None},
    )
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert out == "ok\n"
    assert execute._NATIVE_FAIL_HINT not in out
    assert host_calls == []


# 4. 超时：_NATIVE_FAIL_HINT + "Command timed out"
def test_timeout_prefixes_native_hint(monkeypatch):
    fake, host_calls = _seatbelt_fake(
        monkeypatch,
        {"exit_code": None, "stdout": "", "stderr": "", "timed_out": True, "error": None},
    )
    out = _run(execute.execute_tool("run_shell", {"command": "sleep 99", "timeout": 5000}))
    assert execute._NATIVE_FAIL_HINT in out
    assert "Command timed out after 5000ms" in out
    assert host_calls == []


# 5. backend 不可用：fail-closed（H①）—— 每次都返回 escalate 提示，宿主从不被调用
def test_no_backend_fails_closed_every_call(monkeypatch, capsys):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    _set_backend(monkeypatch, None)
    host_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")

    out1 = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    out2 = _run(execute.execute_tool("run_shell", {"command": "python y.py"}))
    assert "native OS sandbox unavailable" in out1
    assert "native OS sandbox unavailable" in out2
    assert "escalate=true" in out1 and "escalate=true" in out2
    assert host_calls == []  # 从不静默宿主裸跑

    err = capsys.readouterr().err
    assert "UNCONFINED" not in err  # 不再打印 UNCONFINED 警告


# 6. bypassPermissions 不解除沙盒：仍路由到假后端（confined），未走宿主
def test_bypass_permissions_stays_confined(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    fake = _FakeBackend(
        {"exit_code": 0, "stdout": "CONFINED_OK", "stderr": "", "timed_out": False, "error": None}
    )
    _set_backend(monkeypatch, fake)
    host_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")

    inp = {"command": "python x.py"}
    # 权限层：bypassPermissions 直接 allow（不读 runtime），但不改变路由/姿态。
    decision = permissions.check_permission("run_shell", inp, mode="bypassPermissions")
    assert decision["action"] == "allow"
    # 路由层：_route_run_shell 不读 permission_mode → 仍走 seatbelt 后端（confined）。
    out = _run(execute.execute_tool("run_shell", inp))
    assert out == "CONFINED_OK"
    assert len(fake.calls) == 1
    assert fake.calls[0]["posture"] == "workspace-write"
    assert host_calls == []  # 未走宿主



def test_seatbelt_run_text_success(monkeypatch):
    monkeypatch.setattr(
        seatbelt, "run_structured",
        lambda inp, **kw: {"exit_code": 0, "stdout": "hello\n", "stderr": "", "timed_out": False, "error": None},
    )
    assert seatbelt.run({"command": "echo hello"}) == "hello\n"


def test_seatbelt_run_text_no_output(monkeypatch):
    monkeypatch.setattr(
        seatbelt, "run_structured",
        lambda inp, **kw: {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "error": None},
    )
    assert seatbelt.run({"command": "true"}) == "(no output)"


def test_seatbelt_run_text_failure(monkeypatch):
    monkeypatch.setattr(
        seatbelt, "run_structured",
        lambda inp, **kw: {"exit_code": 2, "stdout": "out", "stderr": "err", "timed_out": False, "error": None},
    )
    out = seatbelt.run({"command": "false"})
    assert out == "Command failed (exit code 2)\nStdout: out\nStderr: err"


def test_seatbelt_run_text_timeout(monkeypatch):
    monkeypatch.setattr(
        seatbelt, "run_structured",
        lambda inp, **kw: {"exit_code": None, "stdout": "", "stderr": "", "timed_out": True, "error": None},
    )
    assert seatbelt.run({"command": "sleep 99", "timeout": 5000}) == "Command timed out after 5000ms"


def test_seatbelt_run_text_error(monkeypatch):
    monkeypatch.setattr(
        seatbelt, "run_structured",
        lambda inp, **kw: {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "error": "boom"},
    )
    assert seatbelt.run({"command": "x"}) == "Error: boom"


def test_seatbelt_run_passes_posture_and_cwd(monkeypatch):
    captured = {}

    def fake_structured(inp, *, posture="workspace-write", cwd=None):
        captured["posture"] = posture
        captured["cwd"] = cwd
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False, "error": None}

    monkeypatch.setattr(seatbelt, "run_structured", fake_structured)
    seatbelt.run({"command": "echo ok"}, posture="read-only", cwd="/private/tmp")
    assert captured == {"posture": "read-only", "cwd": "/private/tmp"}


# ─── B. skipif-darwin 集成：flag=seatbelt 真跑 ──────────────────────────────

_smoke = pytest.mark.skipif(
    sys.platform != "darwin" or not seatbelt.is_available(),
    reason="requires macOS with sandbox-exec",
)


@_smoke
def test_integration_echo_in_cwd_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    monkeypatch.chdir(tmp_path)
    out = _run(execute.execute_tool("run_shell", {"command": "echo hi"}))
    assert out == "hi\n"


@_smoke
def test_integration_write_outside_cwd_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "seatbelt")
    monkeypatch.chdir(tmp_path)
    home = os.path.realpath(os.path.expanduser("~"))
    roots = seatbelt._writable_roots(os.path.realpath(str(tmp_path)))
    assert not any(home == r or home.startswith(r + os.sep) for r in roots)
    outside = os.path.join(home, f".nanocode_seatbelt_route_{os.getpid()}_{tmp_path.name}.txt")
    if os.path.exists(outside):
        os.unlink(outside)
    try:
        out = _run(execute.execute_tool("run_shell", {"command": f"echo x > {outside}"}))
        assert "Command failed" in out, out
        assert not os.path.exists(outside)
    finally:
        if os.path.exists(outside):
            os.unlink(outside)
