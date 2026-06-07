"""Task 1: Agent session_id 构造参数 + 子 agent 不覆盖 env + 工具注入。"""

import asyncio
import os

from nanocode.agent.engine import Agent


def _agent(**kw):
    return Agent(api_key="test", trace_enabled=False, **kw)


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
    Agent(api_key="test", trace_enabled=False, is_sub_agent=True, session_id="childsid")
    assert os.environ.get("NANOCODE_SESSION_ID") == "PARENT"


def test_confirmed_paths_injection_shares_reference():
    shared = {"/already/confirmed"}
    a = _agent(confirmed_paths=shared)
    assert a._confirmed_paths is shared


def test_confirmed_paths_default_empty():
    a = _agent()
    assert a._confirmed_paths == set()


def test_execute_tool_call_injects_session_id_sandbox(monkeypatch):
    a = _agent(session_id="injsid")
    captured = {}

    async def fake_run_real(name, inp):
        captured["name"] = name
        captured["inp"] = inp
        return "ok"

    monkeypatch.setattr(a, "_run_real_tool", fake_run_real)
    asyncio.run(a._execute_tool_call("sandbox_shell", {"command": "echo hi"}))
    assert captured["inp"]["_session_id"] == "injsid"


def test_execute_tool_call_injects_session_id_run_shell(monkeypatch):
    a = _agent(session_id="injsid")
    captured = {}

    async def fake_run_real(name, inp):
        captured["inp"] = inp
        return "ok"

    monkeypatch.setattr(a, "_run_real_tool", fake_run_real)
    asyncio.run(a._execute_tool_call("run_shell", {"command": "echo hi"}))
    assert captured["inp"]["_session_id"] == "injsid"


def test_execute_tool_call_does_not_override_existing_session_id(monkeypatch):
    a = _agent(session_id="injsid")
    captured = {}

    async def fake_run_real(name, inp):
        captured["inp"] = inp
        return "ok"

    monkeypatch.setattr(a, "_run_real_tool", fake_run_real)
    asyncio.run(a._execute_tool_call("sandbox_shell",
                                     {"command": "x", "_session_id": "preset"}))
    assert captured["inp"]["_session_id"] == "preset"
