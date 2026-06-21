"""P1: foreground sub-agent safety — wait_for timeout + max_turns ceiling + schema."""

import asyncio
import json

from nanocode.agent.engine import Agent
from nanocode.agent.subagent_manager import SUBAGENT_MAX_TURNS_FALLBACK
from nanocode.tools import tool_definitions
from nanocode.tools.agent import SCHEMA
from nanocode.agents import registry as config


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="fgsid", **kw)


def _spy_build(parent, *, run_once=None, text="fg done",
               tokens=None, capture=None):
    """spy _build_sub_agent: inject a stub run_once and capture build kwargs."""
    tokens = tokens or {"input": 3, "output": 1}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)
        if capture is not None:
            capture["kw"] = kw
            capture["sub"] = sub
        if run_once is not None:
            sub.run_once = run_once(sub)
        else:
            async def _ro(prompt):
                return {"text": text, "tokens": tokens}
            sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


# ─── schema ──────────────────────────────────────────────────


def test_agent_schema_has_timeout_ms():
    props = SCHEMA["input_schema"]["properties"]
    assert "timeout_ms" in props
    assert props["timeout_ms"]["type"] == "integer"


# ─── foreground timeout returns a structured string (no crash) ──


def test_foreground_timeout_returns_string_fresh():
    parent = _agent()

    def _slow(sub):
        async def _ro(prompt):
            await asyncio.sleep(60)
            return {"text": "never", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build(parent, run_once=_slow)

    async def scenario():
        return await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "timeout_ms": 30})

    res = asyncio.run(scenario())
    assert isinstance(res, str)
    assert "timed out" in res.lower()
    assert "30" in res
    # the record exists and is marked timed_out (parent loop did not crash)
    run = json.loads(parent.run_list())[0]
    assert run["status"] == "timed_out"


def test_foreground_timeout_does_not_propagate_exception():
    """Even with a tiny timeout, _execute_agent_tool returns a value, never raises."""
    parent = _agent()

    def _slow(sub):
        async def _ro(prompt):
            await asyncio.sleep(60)
        return _ro

    _spy_build(parent, run_once=_slow)
    # should NOT raise
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "explore", "description": "d", "prompt": "p", "timeout_ms": 20}))
    assert res.startswith("[sub-agent timed out")


def test_foreground_success_no_timeout_returns_text():
    parent = _agent()
    _spy_build(parent, text="hello from sub")
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    # P3: bounded envelope — small text passes through as summary + points at result.md.
    assert "hello from sub" in res
    assert "result.md" in res
    assert json.loads(parent.run_list())[0]["status"] == "completed"


def test_manifest_timeout_used_when_tool_omits(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    d.mkdir(parents=True)
    (d / "slowagent.md").write_text(
        "---\nname: slowagent\ntimeout-ms: 25\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()

    parent = _agent()

    def _slow(sub):
        async def _ro(prompt):
            await asyncio.sleep(60)
        return _ro

    _spy_build(parent, run_once=_slow)
    # no timeout_ms in tool call -> manifest timeout-ms=25 applies
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "slowagent", "description": "d", "prompt": "p"}))
    assert "timed out" in res.lower()
    assert "25" in res


# ─── max_turns ceiling ───────────────────────────────────────


def test_fresh_subagent_gets_fallback_max_turns():
    parent = _agent()
    cap = {}
    _spy_build(parent, capture=cap)
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert cap["kw"]["max_turns"] == SUBAGENT_MAX_TURNS_FALLBACK


def test_manifest_max_turns_used(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    d.mkdir(parents=True)
    (d / "boundagent.md").write_text(
        "---\nname: boundagent\nmax-turns: 4\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()

    parent = _agent()
    cap = {}
    _spy_build(parent, capture=cap)
    asyncio.run(parent._execute_agent_tool(
        {"type": "boundagent", "description": "d", "prompt": "p"}))
    assert cap["kw"]["max_turns"] == 4


def test_max_turns_clamped_to_parent_remaining():
    # parent budget 10, already used 7 -> remaining 3 -> clamp below fallback(50)
    parent = _agent(max_turns=10)
    parent.current_turns = 7
    cap = {}
    _spy_build(parent, capture=cap)
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert cap["kw"]["max_turns"] == 3


def test_bounded_helper_no_parent_budget_uses_value():
    parent = _agent()
    assert parent._subagents.bounded_max_turns(None) == SUBAGENT_MAX_TURNS_FALLBACK
    assert parent._subagents.bounded_max_turns(12) == 12


def test_bounded_helper_clamps_to_parent():
    parent = _agent(max_turns=5)
    parent.current_turns = 2  # remaining 3
    assert parent._subagents.bounded_max_turns(100) == 3
    assert parent._subagents.bounded_max_turns(2) == 2


def test_build_sub_agent_passes_max_turns_and_model():
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder",
        max_turns=9, model="custom-model-x")
    assert sub.max_turns == 9
    assert sub.model == "custom-model-x"


def test_build_sub_agent_defaults_inherit_parent_model():
    parent = _agent(model="parent-model")
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder")
    assert sub.model == "parent-model"
    assert sub.max_turns is None  # not passed -> None (callers pass it explicitly)
