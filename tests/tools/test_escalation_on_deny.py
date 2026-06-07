"""escalation-on-deny：沙盒被拒/失败后请求提权到宿主（NANOCODE_SHELL_SANDBOX=auto）。"""

import asyncio

from nanocode.tools import check_permission
from nanocode.tools import permissions, execute, run_shell, sandbox_shell


def _run(coro):
    return asyncio.run(coro)


# 1. escalate 触发 confirm（auto）
def test_escalate_triggers_confirm(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    r = check_permission("run_shell", {"command": "git status", "escalate": True}, "default")
    assert r["action"] == "confirm"
    assert "escalate" in r["message"]
    assert "host" in r["message"]


# 2. escalate 在 dontAsk 下 deny（auto）
def test_escalate_deny_in_dontask(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    r = check_permission("run_shell", {"command": "git status", "escalate": True}, "dontAsk")
    assert r["action"] == "deny"


# 3. escalate 零回归（off）：flag 未设时 escalate 被忽略，普通 allow（无 confirm、无 runtime）
def test_escalate_zero_regression_off(monkeypatch):
    monkeypatch.delenv("NANOCODE_SHELL_SANDBOX", raising=False)
    r = check_permission("run_shell", {"command": "npm test", "escalate": True}, "default")
    assert r == {"action": "allow"}
    assert "runtime" not in r


# 4. escalate 路由到宿主（auto）：escalate 优先，sandbox_shell.run 不被调用
def test_escalate_routes_to_host(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    host_calls = []
    sbx_calls = []
    monkeypatch.setattr(run_shell, "run", lambda inp: host_calls.append(inp) or "HOST_OK")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: sbx_calls.append(inp) or "SBX_OK")
    # python x.py 本会 classify 成 sandbox；加 escalate=True 后应直接走宿主。
    assert permissions.classify_shell_runtime("python x.py") == "sandbox"
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py", "escalate": True}))
    assert out == "HOST_OK"
    assert len(host_calls) == 1
    assert sbx_calls == []


# 5. 沙盒失败 → 提示出现（auto）：含 escalate=true 字样和原始失败文本
def test_sandbox_failure_emits_hint(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    # 确保命令 classify 成 sandbox（非只读白名单、非危险）。
    assert permissions.classify_shell_runtime("git rev-list HEAD") == "sandbox"
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    fail_text = "Command failed (exit code 127)\nStderr:\n/bin/sh: git: not found"
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: fail_text)
    out = _run(execute.execute_tool("run_shell", {"command": "git rev-list HEAD"}))
    assert "escalate=true" in out
    assert fail_text in out
    assert out.startswith(execute._SANDBOX_FAIL_HINT)


# 6. 沙盒成功 → 无提示（auto）：不污染成功输出
def test_sandbox_success_no_hint(monkeypatch):
    monkeypatch.setenv("NANOCODE_SHELL_SANDBOX", "auto")
    assert permissions.classify_shell_runtime("python x.py") == "sandbox"
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: "/fake/msb")
    monkeypatch.setattr(sandbox_shell, "run", lambda inp: "hello\n")
    out = _run(execute.execute_tool("run_shell", {"command": "python x.py"}))
    assert out == "hello\n"
    assert execute._SANDBOX_FAIL_HINT not in out


# 7. escalate 的 SCHEMA 存在且不在 required
def test_escalate_in_schema():
    props = run_shell.SCHEMA["input_schema"]["properties"]
    assert "escalate" in props
    assert props["escalate"]["type"] == "boolean"
    assert "escalate" not in run_shell.SCHEMA["input_schema"]["required"]
