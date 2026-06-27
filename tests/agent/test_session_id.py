"""Agent session_id 构造参数 + 子 agent 不覆盖 env + HostContext 边界（docs/19）。

docs/19：`_session_id` / `_cwd` 不再注入 tool input——它们经 HostContext 由 runtime 提供，
模型无法 spoof（validator 拒下划线键）。本文件验证身份/边界，不再验证已删的注入机制。
"""

import asyncio
import os

from nanocode.agent.engine import Agent


def _agent(**kw):
    return Agent(api_key="test", **kw)


def test_agent_adopts_session_id():
    a = _agent(session_id="adopt99")
    assert a.session_id == "adopt99"


def test_agent_generates_session_id_when_absent():
    a = _agent()
    assert a.session_id and isinstance(a.session_id, str)


def test_main_agent_writes_env():
    a = _agent(session_id="mainsid")
    assert os.environ.get("NANOCODE_SESSION_ID") == "mainsid"


def test_sub_agent_does_not_overwrite_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_SESSION_ID", "PARENT")
    Agent(api_key="test", is_sub_agent=True, session_id="childsid")
    assert os.environ.get("NANOCODE_SESSION_ID") == "PARENT"


def test_confirmed_paths_injection_shares_reference():
    shared = {"/already/confirmed"}
    a = _agent(confirmed_paths=shared)
    assert a._confirmed_paths is shared


def test_confirmed_paths_default_empty():
    a = _agent()
    assert a._confirmed_paths == set()


# ─── docs/19：session_id / cwd 经 HostContext，不再注入 tool input ──────────────

def test_host_context_carries_session_id():
    a = _agent(session_id="hcsid")
    assert a.host_context().session_id == "hcsid"


def test_run_shell_input_has_no_injected_keys(monkeypatch, tmp_path):
    """run_shell 抵达 SandboxManager 时，HostContext 持 cwd/session；inp 不被注入 _cwd/_session_id。"""
    a = _agent(session_id="injsid", permission_mode="bypassPermissions")
    ctx = a.mint_tool_context("run_shell")
    assert ctx.exec is not None
    assert not hasattr(ctx.exec, "_host")
    captured = {}

    async def fake_exec(request, host, policy, approval):
        captured["request"] = request
        captured["host"] = host
        return "ok"

    monkeypatch.setattr(a._sandbox, "execute_shell", fake_exec)
    out = asyncio.run(a._execute_tool_call("run_shell", {"command": "echo hi"}))
    assert out == "ok"
    # ShellRequest 是 typed（无 _cwd/_session_id 字段）；session 来自 HostContext。
    assert captured["request"].command == "echo hi"
    assert captured["host"].session_id == "injsid"


def test_model_cannot_spoof_cwd():
    # validator 在 permission 之前拒下划线键（_cwd/_session_id），不 silent strip。
    a = _agent(session_id="s", permission_mode="bypassPermissions")
    out = asyncio.run(a._execute_tool_call("run_shell", {"command": "pwd", "_cwd": "/"}))
    assert "Error" in out and "_cwd" in out
