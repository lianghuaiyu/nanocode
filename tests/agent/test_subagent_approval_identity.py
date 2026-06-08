"""P4: approval-UI identity — a sub-agent's confirmation message carries its
identity (agent id + type + source). Main-agent behavior is unchanged."""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.tools import tool_definitions
from nanocode.subagents import config


def _agent(**kw):
    kw.setdefault("permission_mode", "default")
    return Agent(api_key="test", trace_enabled=False, session_id="apsid", **kw)


def test_subagent_confirm_message_contains_identity():
    captured = {}

    async def spy_confirm(message):
        captured["message"] = message
        return True

    parent = _agent(confirm_fn=spy_confirm)
    cfg = config.get_sub_agent_config("coder")
    sub = parent._build_sub_agent(
        system_prompt="s", tools=cfg["tools"], agent_type="coder",
        artifact_id="agent-042")
    ok = asyncio.run(sub._confirm_dangerous("rm -rf /tmp/x"))
    assert ok is True
    msg = captured["message"]
    assert "agent-042" in msg          # the agent id
    assert "type=coder" in msg         # the agent type
    assert "rm -rf /tmp/x" in msg      # original command preserved


def test_subagent_confirm_message_includes_source(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    d.mkdir(parents=True)
    (d / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: r\n---\nReview.")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()

    captured = {}

    async def spy_confirm(message):
        captured["message"] = message
        return False

    parent = _agent(confirm_fn=spy_confirm)
    cfg = config.get_sub_agent_config("reviewer")
    sub = parent._build_sub_agent(
        system_prompt=cfg["system_prompt"], tools=cfg["tools"],
        agent_type="reviewer", artifact_id="agent-007",
        agent_source=cfg.get("source"))
    asyncio.run(sub._confirm_dangerous("dangerous-cmd"))
    msg = captured["message"]
    assert "agent-007" in msg
    assert "type=reviewer" in msg
    assert "/reviewer.md" in msg       # the source path for the custom project agent


def test_main_agent_confirm_message_unchanged():
    captured = {}

    async def spy_confirm(message):
        captured["message"] = message
        return True

    parent = _agent(confirm_fn=spy_confirm)
    assert parent.is_sub_agent is False
    asyncio.run(parent._confirm_dangerous("rm -rf /tmp/y"))
    # main agent: message is the raw command, no identity decoration
    assert captured["message"] == "rm -rf /tmp/y"


def test_decorate_helper_directly():
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tool_definitions, agent_type="coder",
        artifact_id="agent-099")
    decorated = sub._decorate_confirm_message("git push --force")
    assert decorated.startswith("[sub-agent agent-099 type=coder]")
    assert decorated.endswith("git push --force")
    # main agent passes through unchanged
    assert parent._decorate_confirm_message("git push --force") == "git push --force"


# ─── Codex P4 regression: approval dedupe is identity-scoped for sub-agents ──


def test_confirm_dedupe_key_is_identity_scoped_for_subagents():
    """A sibling sub-agent's prior approval of a raw message must NOT let another
    sub-agent skip its own identity-bearing confirmation (shared _confirmed_paths)."""
    parent = Agent(api_key="test", trace_enabled=False, permission_mode="bypassPermissions",
                   session_id="dedupe")
    a = parent._build_sub_agent(system_prompt="s", tools=parent.tools,
                                agent_type="coder", artifact_id="agent-001")
    b = parent._build_sub_agent(system_prompt="s", tools=parent.tools,
                                agent_type="coder", artifact_id="agent-002")
    msg = "rm -rf something"
    ka = a._confirm_dedupe_key(msg)
    kb = b._confirm_dedupe_key(msg)
    assert ka != kb            # different sub-agents -> different dedupe keys
    assert "agent-001" in ka and "agent-002" in kb
    # main agent uses the raw message (unchanged dedupe behavior)
    assert parent._confirm_dedupe_key(msg) == msg
