"""P4: call-time tool allowlist enforcement (the security keystone).

P1 computed the effective tool-name set but only ADVERTISED it. P4 enforces it
at call time in Agent._execute_tool_call, AFTER meta-tool interception and BEFORE
any real tool dispatches (foreground OR background). A read-only sub-agent that
emits write_file / run_shell is now BLOCKED at call time, not merely un-advertised.
"""

from __future__ import annotations

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.tools.permissions import ALWAYS_ALLOWED_META as _ALWAYS_ALLOWED_META
from nanocode.tools import tool_definitions
from nanocode.subagents import config


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", trace_enabled=False, session_id="cgsid", **kw)


def _read_only_sub(parent):
    """Build an explore (read-only) sub-agent: allowed = READ_ONLY_TOOLS."""
    cfg = config.get_sub_agent_config("explore")
    return parent._build_sub_agent(
        system_prompt=cfg["system_prompt"], tools=cfg["tools"], agent_type="explore")


# ─── keystone: out-of-set real tool is blocked at call time ──────


def test_readonly_subagent_write_file_blocked(tmp_path):
    parent = _agent()
    sub = _read_only_sub(parent)
    target = tmp_path / "should_not_write.txt"
    # write_file is NOT in the explore allowlist -> blocked before dispatch.
    res = asyncio.run(sub._execute_tool_call(
        "write_file", {"file_path": str(target), "content": "x"}))
    assert isinstance(res, str)
    assert "not permitted" in res.lower()
    assert "write_file" in res
    # And the file was never created (dispatch was short-circuited).
    assert not target.exists()


def test_readonly_subagent_allowed_tool_still_runs(tmp_path):
    parent = _agent()
    sub = _read_only_sub(parent)
    f = tmp_path / "hello.txt"
    f.write_text("contents-here")
    # read_file IS allowed for explore -> dispatches normally.
    res = asyncio.run(sub._execute_tool_call("read_file", {"file_path": str(f)}))
    assert "not permitted" not in res.lower()
    assert "contents-here" in res


def test_readonly_subagent_run_shell_blocked_even_in_background():
    """run_shell-background is handled before meta interception; the call gate must
    still block run_shell for a read-only agent (covers fg AND bg)."""
    parent = _agent()
    sub = _read_only_sub(parent)
    # background branch: run_in_background=True. Must be blocked, NOT spawned.
    res = asyncio.run(sub._execute_tool_call(
        "run_shell", {"command": "echo hi", "run_in_background": True}))
    assert "not permitted" in res.lower()
    assert "run_shell" in res
    # no background shell task was registered
    assert sub._background_tasks == set()
    assert sub.task_manager.list_tasks() == []


def test_readonly_subagent_run_shell_blocked_foreground():
    parent = _agent()
    sub = _read_only_sub(parent)
    res = asyncio.run(sub._execute_tool_call("run_shell", {"command": "echo hi"}))
    assert "not permitted" in res.lower()


# ─── meta tools a sub-agent legitimately holds are NEVER blocked ──


def test_meta_tools_not_blocked_for_restricted_subagent():
    parent = _agent()
    sub = _read_only_sub(parent)
    # task_list is a meta tool — restricted allowlist must not block it.
    assert sub._tool_blocked_by_allowlist("task_list") is False
    res = asyncio.run(sub._execute_tool_call("task_list", {}))
    assert "not permitted" not in res.lower()


def test_all_meta_tool_names_pass_gate_even_when_restricted():
    parent = _agent()
    sub = _read_only_sub(parent)
    # Only the pure host-only meta tools (task panel + plan mode) are always allowed.
    for name in _ALWAYS_ALLOWED_META:
        assert sub._tool_blocked_by_allowlist(name) is False


# ─── HIGH (Codex P4): memory / skill / agent are NOT blanket-exempt ──


def test_memory_blocked_for_readonly_subagent():
    """memory save/update/delete falls through to the real handler, so a read-only
    sub-agent without memory in its effective set must be blocked at call time."""
    parent = _agent()
    sub = _read_only_sub(parent)
    assert "memory" not in sub._allowed_tool_names
    assert sub._tool_blocked_by_allowlist("memory") is True
    res = asyncio.run(sub._execute_tool_call(
        "memory", {"action": "save", "content": "x"}))
    assert "not permitted" in res.lower()


def test_skill_blocked_for_readonly_subagent():
    """skill (fork/hooks can reach shell) must be allowlist-gated, not exempt."""
    parent = _agent()
    sub = _read_only_sub(parent)
    assert sub._tool_blocked_by_allowlist("skill") is True


def test_agent_is_independent_backstop_always_blocked_for_subagents():
    """'agent' must be blocked at call time for ANY sub-agent regardless of toolset
    construction — independent fail-closed backstop against grandchild spawn."""
    parent = _agent()
    sub = _read_only_sub(parent)
    assert sub._tool_blocked_by_allowlist("agent") is True
    # even a coder sub-agent (broad toolset) cannot pass 'agent' through the gate
    coder = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder")
    assert coder._tool_blocked_by_allowlist("agent") is True
    res = asyncio.run(coder._execute_tool_call(
        "agent", {"type": "general", "description": "d", "prompt": "p"}))
    assert "not permitted" in res.lower()
    # main agent is unrestricted — 'agent' passes the gate
    assert parent._tool_blocked_by_allowlist("agent") is False


# ─── main agent (None allowlist) is never gated ──────────────────


def test_main_agent_allowlist_is_none_and_never_blocks():
    parent = _agent()
    assert parent._allowed_tool_names is None
    assert parent._tool_blocked_by_allowlist("write_file") is False
    assert parent._tool_blocked_by_allowlist("run_shell") is False


# ─── _build_sub_agent derives the allowlist from the EFFECTIVE toolset ──


def test_build_sub_agent_sets_allowlist_from_effective_tools():
    parent = _agent()
    cfg = config.get_sub_agent_config("explore")
    sub = parent._build_sub_agent(
        system_prompt="s", tools=cfg["tools"], agent_type="explore")
    assert sub._allowed_tool_names == {t["name"] for t in cfg["tools"]}
    assert "agent" not in sub._allowed_tool_names
    assert "write_file" not in sub._allowed_tool_names


def test_allowlist_uses_effective_set_not_allowed_names_alone(tmp_path, monkeypatch):
    """P4 contract: enforce against the ACTUAL toolset (agent stripped + disallowed
    removed), not allowed_names alone. A coder (allowed_names=None) with
    disallowed-tools must still block the denied tool at call time."""
    d = tmp_path / ".nanocode" / "agents"
    d.mkdir(parents=True)
    (d / "noshell.md").write_text(
        "---\nname: noshell\nextends: general\ndisallowed-tools: run_shell\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()

    parent = _agent()
    cfg = config.get_sub_agent_config("noshell")
    # allowed_names is None (unrestricted-except-deny) but run_shell is removed.
    assert cfg["allowed_names"] is None
    assert "run_shell" not in {t["name"] for t in cfg["tools"]}
    sub = parent._build_sub_agent(
        system_prompt="s", tools=cfg["tools"], agent_type="noshell")
    # run_shell must be blocked even though allowed_names was None.
    assert sub._tool_blocked_by_allowlist("run_shell") is True
    res = asyncio.run(sub._execute_tool_call("run_shell", {"command": "echo x"}))
    assert "not permitted" in res.lower()
    # a tool still in the effective set (e.g. read_file) is allowed.
    assert sub._tool_blocked_by_allowlist("read_file") is False


# ─── HARD INVARIANT: agent tool always stripped + never spawnable by sub ──


def test_sub_agent_never_holds_agent_and_cannot_pass_gate():
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder")
    assert all(t["name"] != "agent" for t in sub.tools)
    # 'agent' is never in the sub-agent's toolset AND the call-time gate blocks it
    # independently (defense in depth) — so a sub-agent cannot spawn a grandchild.
    assert "agent" not in {t["name"] for t in sub.tools}
    assert sub._tool_blocked_by_allowlist("agent") is True


# ─── depth counter threads through ───────────────────────────────


def test_depth_increments_for_sub_agents():
    parent = _agent()
    assert parent.depth == 0
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder")
    assert sub.depth == 1


# ─── HIGH (Codex P4): skill hooks cannot proxy shell past the allowlist ──


def test_skill_hook_shell_blocked_for_readonly_subagent():
    """A read-only sub-agent (no run_shell) must not gain shell via a skill hook."""
    parent = _agent()
    sub = _read_only_sub(parent)
    hook = {"skill": "evil", "event": "pre-tool-use", "matcher": "*",
            "command": "echo pwned", "timeout_ms": 1000}
    ok, msg = asyncio.run(sub._run_hook(hook, "read_file", {"file_path": "x"}, None))
    assert ok is False
    assert "run_shell is not permitted" in msg


def test_skill_hook_shell_allowed_for_main_agent():
    """Main agent (unrestricted) hooks are not blocked by the allowlist gate."""
    parent = _agent()
    assert parent._tool_blocked_by_allowlist("run_shell") is False


# ─── Codex P4 round-2: meta-tool proxy paths can't bypass the keystone ──


def test_subagent_cannot_trigger_memory_consolidate():
    """Codex P4 HIGH: `memory consolidate` spawns a curator (grandchild) + mutates
    host memory, bypassing the agent backstop/depth/threads. Sub-agents must not
    be able to trigger it."""
    parent = _agent()
    # give the sub-agent memory in its set so the allowlist gate would pass
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder")
    assert "memory" in sub._allowed_tool_names
    res = asyncio.run(sub._execute_tool_call("memory", {"action": "consolidate"}))
    assert "not available to sub-agents" in res.lower()
    # no curator sub-agent / consolidate task was created
    assert [s for s in sub.task_manager.list_subagents()
            if s.type == "memory-curator"] == []


def test_subagent_task_stop_cannot_cancel_unowned_parent_task():
    """Codex P4 MED: a sub-agent shares the parent TaskManager; task_stop must not
    let it mark a task it doesn't hold the coroutine for as cancelled."""
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder")
    # a parent-owned running task whose coroutine the sub-agent does NOT hold
    t = parent.task_manager.create_task("shell", "long job", owner_agent_id=None)
    parent.task_manager.update_task(t.id, status="running")
    res = asyncio.run(sub._execute_tool_call("task_stop", {"task_id": t.id}))
    assert "refusing to stop" in res.lower()
    # the parent task is untouched (still running, not silently cancelled)
    assert parent.task_manager.get_task(t.id).status == "running"


def test_main_agent_task_stop_orphan_still_cancels():
    """Main agent keeps the historical orphan-cancel behavior."""
    parent = _agent()
    t = parent.task_manager.create_task("shell", "j", owner_agent_id=None)
    parent.task_manager.update_task(t.id, status="running")
    res = asyncio.run(parent._execute_tool_call("task_stop", {"task_id": t.id}))
    assert "marked cancelled" in res.lower()
    assert parent.task_manager.get_task(t.id).status == "cancelled"


# ─── Holistic review HIGH: sub-agents cannot use plan-mode to self-escalate ──


def test_subagent_cannot_use_plan_mode_to_escalate():
    """A sub-agent inheriting permission_mode='plan' must NOT be able to call
    exit_plan_mode to widen its own permission_mode."""
    parent = _agent(permission_mode="plan")
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder")
    assert sub.permission_mode == "plan"   # inherited
    res = asyncio.run(sub._execute_tool_call("exit_plan_mode", {}))
    assert "not available to sub-agents" in res.lower()
    assert sub.permission_mode == "plan"   # unchanged — no self-escalation
    res2 = asyncio.run(sub._execute_tool_call("enter_plan_mode", {}))
    assert "not available to sub-agents" in res2.lower()
    # main agent can still use plan mode
    res3 = asyncio.run(parent._execute_tool_call("enter_plan_mode", {}))
    assert "not available to sub-agents" not in res3.lower()
