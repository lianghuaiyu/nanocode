"""Spec B: hook 命令走统一 check_permission（不再只靠 is_dangerous 黑名单直跑）。

deny 规则阻断、confirm 前台询问/后台自动拒、bypass 下危险命令硬底线阻断。
关键：monkeypatch `run_shell.run_structured`（engine 内 `from ..tools import run_shell`
持有的那个模块函数）为记录调用的 stub，使测试永不真正执行命令。"""

import asyncio

import pytest

from nanocode.agent.engine import Agent, _auto_deny_confirm
from nanocode.tools import run_shell, permissions


def _agent(**kw):
    return Agent(api_key="test", trace_enabled=False, **kw)


def _hook(cmd):
    return {"skill": "t", "event": "pre-tool-use", "matcher": "*",
            "command": cmd, "timeout_ms": 3000}


@pytest.fixture
def stub_run_structured(monkeypatch):
    """记录调用并返回成功结果的 stub；确保命令永不真跑。"""
    calls = []

    def _stub(inp):
        calls.append(inp)
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False, "error": None}

    monkeypatch.setattr(run_shell, "run_structured", _stub)
    return calls


async def _true_confirm(_command: str) -> bool:
    return True


async def _false_confirm(_command: str) -> bool:
    return False


def test_normal_command_allowed(stub_run_structured):
    """普通命令放行：echo hi / default / 无 confirm_fn → (True, "")，stub 被调用。"""
    a = _agent()
    a.confirm_fn = None
    ok, msg = asyncio.run(a._run_hook(_hook("echo hi"), "read_file", {"file_path": "x"}, None))
    assert ok is True
    assert msg == ""
    assert len(stub_run_structured) == 1
    assert stub_run_structured[0]["command"] == "echo hi"


def test_deny_rule_blocks(stub_run_structured, monkeypatch):
    """deny 规则阻断：注入 deny=["run_shell(echo *)"]，echo hi → (False,...)，stub 未调用。"""
    monkeypatch.setattr(
        permissions, "_cached_rules",
        {"allow": [], "deny": [{"tool": "run_shell", "pattern": "echo *"}]},
    )
    a = _agent()
    ok, msg = asyncio.run(a._run_hook(_hook("echo hi"), "read_file", {"file_path": "x"}, None))
    assert ok is False
    assert "denied" in msg.lower()
    assert stub_run_structured == []


def test_dangerous_foreground_confirm_approved(stub_run_structured):
    """危险命令前台确认→批准则执行：rm -rf /tmp/x / default / confirm_fn=True
    → (True, "")，stub 被调用（确认 stub 拦住，未真跑 rm）。"""
    a = _agent()
    a.confirm_fn = _true_confirm
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /tmp/x"), "read_file", {"file_path": "x"}, None))
    assert ok is True
    assert msg == ""
    assert len(stub_run_structured) == 1
    assert stub_run_structured[0]["command"] == "rm -rf /tmp/x"


def test_dangerous_foreground_confirm_rejected(stub_run_structured):
    """危险命令前台确认→拒绝则阻断：同上但 confirm_fn=False → (False,...)，stub 未调用。"""
    a = _agent()
    a.confirm_fn = _false_confirm
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /tmp/x"), "read_file", {"file_path": "x"}, None))
    assert ok is False
    assert "not approved" in msg.lower()
    assert stub_run_structured == []


def test_bypass_dangerous_hard_backstop(stub_run_structured):
    """bypassPermissions 下危险命令硬底线阻断：rm -rf / / bypass / confirm_fn=True
    → (False, ...safety backstop...)，stub 未调用（证明 bypass 不放行危险 hook）。"""
    a = _agent(permission_mode="bypassPermissions")
    a.confirm_fn = _true_confirm
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /"), "read_file", {"file_path": "x"}, None))
    assert ok is False
    assert "safety backstop" in msg.lower()
    assert stub_run_structured == []


def test_background_auto_deny(stub_run_structured):
    """后台自动拒绝：confirm_fn=_auto_deny_confirm（恒拒 async），危险命令
    → (False,...)，stub 未调用。"""
    a = _agent()
    a.confirm_fn = _auto_deny_confirm
    ok, msg = asyncio.run(a._run_hook(_hook("rm -rf /tmp/y"), "read_file", {"file_path": "x"}, None))
    assert ok is False
    assert "not approved" in msg.lower()
    assert stub_run_structured == []
