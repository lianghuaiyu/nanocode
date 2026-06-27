"""P1 security regressions (Codex cross-review findings).

Encodes the exact privilege-escalation / unbounded-loop paths found during the
Codex cross-validation of Phase 1, so they cannot silently regress:

  HIGH-1  empty/[] sub-agent toolset must NOT widen to the full global table.
  HIGH-3  background + skill-fork sub-agents must be bounded by max_turns.
  HIGH-4  reserved curator records must not be resumable via the agent tool.
  MED     a failed `extends` (cycle/missing/reserved) must FAIL CLOSED, never
          fall back to the child's own (possibly wider) allow-list.
"""

from __future__ import annotations

import asyncio
import json

from nanocode.agent.engine import Agent
from nanocode.agent.subagent_manager import SUBAGENT_MAX_TURNS_FALLBACK
from nanocode.tools import REGISTRY
from nanocode.agents import registry as config

tool_definitions = REGISTRY.schemas()


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="secsid", **kw)


def _write(d, name, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body)


# ─── HIGH-1: empty toolset must not widen to all tools ───────────


def test_empty_custom_tools_does_not_widen_to_all():
    # Agent.__init__: custom_tools=[] must stay [] (None means "main agent default").
    a = Agent(api_key="test", is_sub_agent=True, custom_tools=[])
    assert a.tools == []
    names = {t["name"] for t in a.tools}
    assert "agent" not in names
    assert "run_shell" not in names


def test_main_agent_none_tools_still_gets_full_table():
    a = Agent(api_key="test")
    assert a.tools == tool_definitions


def test_build_sub_agent_with_empty_tools_yields_no_tools():
    parent = _agent()
    sub = parent._build_sub_agent(system_prompt="x", tools=[], agent_type="coder")
    assert sub.tools == []


def test_curator_profile_is_toolless_and_does_not_widen():
    # reserved curators are toolless; the empty list must stay empty.
    profile = config.build_profile(config.MEMORY_CURATOR_TYPE)
    tools = config.effective_tools(profile)
    assert tools == []
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt=profile.prompt, tools=tools,
        agent_type=config.MEMORY_CURATOR_TYPE, background=True)
    assert sub.tools == []


def test_allowed_tools_agent_cannot_smuggle_agent_tool(tmp_path, monkeypatch):
    # Even if a manifest explicitly allows only 'agent', the effective set is empty
    # (agent is always stripped) and must not widen.
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "smuggle", "---\nname: smuggle\nallowed-tools: agent\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    profile = config.build_profile("smuggle")
    tools = config.effective_tools(profile)
    assert tools == []
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt=profile.prompt, tools=tools, agent_type="smuggle")
    assert {t["name"] for t in sub.tools} == set()


# ─── HIGH-3: background + skill-fork bounded by max_turns ────────


def test_background_subagent_gets_bounded_max_turns(monkeypatch):
    parent = _agent()
    cap = {}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        cap["kw"] = kw
        sub = real_build(**kw)

        async def _ro(prompt):
            return {"text": "bg", "tokens": {"input": 0, "output": 0}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy

    async def scenario():
        tid = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        # let the detached coroutine run to completion
        await asyncio.gather(*list(parent._background_tasks), return_exceptions=True)
        return tid

    asyncio.run(scenario())
    assert cap["kw"].get("max_turns") == SUBAGENT_MAX_TURNS_FALLBACK


# ─── HIGH-4: reserved curator records not resumable via agent tool ──


def test_resume_reserved_curator_record_is_rejected():
    parent = _agent()
    from nanocode.session.lease import SessionLease
    from nanocode.session.manager import SessionManager
    from nanocode.subagents import run_record

    parent._session_mgr = SessionManager.create(parent.session_id)
    child_id = "sess_reserved_curator"
    lease = SessionLease.open_or_create(
        child_id, spawned_by={"sessionId": parent.session_id,
                              "entryId": parent._session_mgr.get_leaf(),
                              "taskId": child_id, "agentId": child_id})
    lease.manager.rewrite_file()
    lease.close()
    run_record.create_run_record(
        child_session_id=child_id,
        parent_session_id=parent.session_id,
        spawn_entry_id=parent._session_mgr.get_leaf(),
        tool_call_id=None,
        agent_type=config.MEMORY_CURATOR_TYPE,
        description="memory curator",
        background=True,
        context_mode="fresh",
        isolation="shared",
        worktree_path=None,
        model={"provider": parent._current_provider(), "modelId": parent.model},
        prompt="curate",
    )
    res = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "prompt": "evil", "resume": child_id}))
    assert isinstance(res, str)
    assert "reserved" in res.lower()
    assert "cannot be resumed" in res.lower()


# ─── MED: failed extends fails closed (no widening) ─────────────


def test_extends_unknown_base_fails_closed_not_to_child_allowlist(tmp_path, monkeypatch):
    # `extends: explroe` (typo) + a wide allowed-tools must NOT grant those tools.
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "typo", "---\nname: typo\nextends: explroe\n"
                      "allowed-tools: run_shell,write_file,read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    names = {t["name"] for t in config.effective_tools(config.build_profile("typo"))}
    assert "run_shell" not in names    # would have leaked under fail-open
    assert "write_file" not in names
    # fail-closed floor is read-only ∩ self-allow -> read_file survives, nothing dangerous
    assert names <= config.READ_ONLY_TOOLS


def test_extends_cycle_fails_closed(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "p", "---\nname: p\nextends: q\nallowed-tools: run_shell,read_file\n---\nbody")
    _write(d, "q", "---\nname: q\nextends: p\nallowed-tools: run_shell,read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    names = {t["name"] for t in config.effective_tools(config.build_profile("p"))}
    assert "run_shell" not in names
    assert names <= config.READ_ONLY_TOOLS


# ─── NEW-MED: effective model recorded on run record ────────


def test_fresh_record_stores_effective_manifest_model(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "fastagent", "---\nname: fastagent\nmodel: claude-haiku-4-5\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()

    parent = _agent()  # parent.model is the default, NOT haiku
    cap = {}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        cap["model"] = kw.get("model")
        sub = real_build(**kw)

        async def _ro(prompt):
            return {"text": "ok", "tokens": {"input": 0, "output": 0}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    asyncio.run(parent._execute_agent_tool(
        {"type": "fastagent", "description": "d", "prompt": "p"}))
    # the sub-agent ran with the manifest model, and the run record stores it (not parent's)
    assert cap["model"] == "claude-haiku-4-5"
    rec = json.loads(parent.run_list())[0]
    assert rec["model"]["modelId"] == "claude-haiku-4-5"
    assert rec["model"]["modelId"] != parent.model
