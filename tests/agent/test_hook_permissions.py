"""hook 命令的权限门 + SandboxManager 执行（docs/19）。

hook 命令先过 check_permission（deny / confirm / 危险硬底线），再经唯一规划点
SandboxManager.execute_structured（HostContext(is_hook=True)）受限执行。
stub 掉 manager 的 execute_structured 以验证门控不真跑命令。
"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.runtime.spawn import _auto_deny_confirm
from nanocode.tools import permissions


def _agent(**kw):
    return Agent(api_key="test", **kw)


def _hook(cmd):
    return {"skill": "t", "event": "pre-tool-use", "matcher": "*",
            "command": cmd, "timeout_ms": 3000}


def _stub_exec(a, *, exit_code=0, error=None, timed_out=False, blocked=None):
    """记录 manager.execute_structured 调用并返回受控结果（命令永不真跑）。"""
    calls = []

    async def fake(request, host, policy, approval):
        calls.append({"request": request, "host": host, "policy": policy, "approval": approval})
        d = {"exit_code": exit_code, "stdout": "ok", "stderr": "",
             "timed_out": timed_out, "error": error}
        if blocked is not None:
            d["blocked"] = blocked
        return d

    a._sandbox.execute_structured = fake
    return calls


async def _true_confirm(_command: str) -> bool:
    return True


async def _false_confirm(_command: str) -> bool:
    return False


# ─── 权限门（执行前）────────────────────────────────────────────

def test_normal_command_allowed():
    a = _agent(); a.confirm_fn = None
    calls = _stub_exec(a)
    ok, msg = asyncio.run(a._run_hook(_hook("echo hi"), "read_file", {"file_path": "x"}, None))
    assert ok is True and msg == ""
    assert len(calls) == 1
    assert calls[0]["request"].command == "echo hi"
    assert calls[0]["host"].is_hook is True            # HostContext(is_hook=True)


def test_deny_rule_blocks(monkeypatch):
    monkeypatch.setattr(
        permissions, "_cached_rules",
        {"allow": [], "deny": [{"tool": "run_shell", "pattern": "echo *"}]})
    a = _agent(); calls = _stub_exec(a)
    ok, msg = asyncio.run(a._run_hook(_hook("echo hi"), "read_file", {"file_path": "x"}, None))
    assert ok is False and "denied" in msg.lower()
    assert calls == []


def test_dangerous_foreground_confirm_approved():
    a = _agent(); a.confirm_fn = _true_confirm
    calls = _stub_exec(a)
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /tmp/x"), "read_file", {"file_path": "x"}, None))
    assert ok is True and msg == ""
    assert len(calls) == 1


def test_dangerous_foreground_confirm_rejected():
    a = _agent(); a.confirm_fn = _false_confirm
    calls = _stub_exec(a)
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /tmp/x"), "read_file", {"file_path": "x"}, None))
    assert ok is False and "not approved" in msg.lower()
    assert calls == []


def test_bypass_dangerous_hard_backstop():
    a = _agent(permission_mode="bypassPermissions"); a.confirm_fn = _true_confirm
    calls = _stub_exec(a)
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /"), "read_file", {"file_path": "x"}, None))
    assert ok is False and "safety backstop" in msg.lower()
    assert calls == []


def test_background_auto_deny():
    a = _agent(); a.confirm_fn = _auto_deny_confirm
    calls = _stub_exec(a)
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /tmp/y"), "read_file", {"file_path": "x"}, None))
    assert ok is False and "not approved" in msg.lower()
    assert calls == []


# ─── SandboxManager 执行 ────────────────────────────────────────

def test_hook_blocked_propagates():
    """manager 返回 blocked（无后端 / VM 不可用）→ hook (False, 'hook blocked: ...')。"""
    a = _agent(); a.confirm_fn = None
    _stub_exec(a, blocked="no sandbox backend can enforce this policy")
    ok, msg = asyncio.run(a._run_hook(_hook("touch out.txt"), "write_file", {"file_path": "out.txt"}, None))
    assert ok is False and "hook blocked" in msg.lower()


def test_hook_failure_reports_exit():
    a = _agent(); a.confirm_fn = None
    _stub_exec(a, exit_code=3)
    ok, msg = asyncio.run(a._run_hook(_hook("touch out.txt"), "write_file", {"file_path": "out.txt"}, None))
    assert ok is False and "exit 3" in msg


def test_hook_allowlist_fail_closed():
    """子 agent 有效集不含 run_shell → hook 不得借 shell 旁路（allowlist fail-closed）。"""
    a = _agent(is_sub_agent=True, allowed_tool_names={"read_file"})
    calls = _stub_exec(a)
    ok, msg = asyncio.run(a._run_hook(_hook("echo hi"), "read_file", {"file_path": "x"}, None))
    assert ok is False and "not permitted" in msg.lower()
    assert calls == []


def test_hook_runs_confined_real(tmp_path, monkeypatch):
    """集成：默认 profile + 本机 native（seatbelt）→ hook 在沙盒内受限实跑（不裸跑宿主）。"""
    from nanocode.tools.sandbox_backends import resolve_native_backend
    if resolve_native_backend() is None:
        pytest.skip("no native sandbox backend on this host")
    monkeypatch.chdir(tmp_path)
    a = _agent(session_id="hooksid"); a.confirm_fn = None
    ok, msg = asyncio.run(a._run_hook(_hook("echo hooked > marker.txt"),
                                      "write_file", {"file_path": "marker.txt"}, None))
    assert ok is True, msg
    assert (tmp_path / "marker.txt").read_text().strip() == "hooked"
