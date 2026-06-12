"""P4: approval-UI identity — a sub-agent's confirmation message carries its
identity (agent id + type + source). Main-agent behavior is unchanged."""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.tools import tool_definitions
from nanocode.agents import registry as config


def _agent(**kw):
    kw.setdefault("permission_mode", "default")
    return Agent(api_key="test", session_id="apsid", **kw)


def test_subagent_confirm_message_contains_identity():
    captured = {}

    async def spy_confirm(message):
        captured["message"] = message
        return True

    parent = _agent(confirm_fn=spy_confirm)
    tools = config.effective_tools(config.build_profile("coder"))
    sub = parent._build_sub_agent(
        system_prompt="s", tools=tools, agent_type="coder",
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
    profile = config.build_profile("reviewer")
    sub = parent._build_sub_agent(
        system_prompt=profile.prompt, tools=config.effective_tools(profile),
        agent_type="reviewer", artifact_id="agent-007",
        agent_source=profile.source)
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
    parent = Agent(api_key="test", permission_mode="bypassPermissions",
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


# ─── Holistic review HIGH: identity-scoped dedupe is enforced end-to-end ──


def test_confirm_if_needed_dedupes_within_agent_but_not_across_siblings():
    """End-to-end on the shared decision both backends call: a sibling sub-agent's
    prior approval of the SAME raw message must NOT let another sub-agent skip its
    own identity-bearing confirmation. (Reverting to raw-message dedupe fails this.)"""
    shared = set()
    parent = Agent(api_key="test", permission_mode="default",
                   session_id="apdedupe", confirmed_paths=shared)
    prompts = []

    def make(artifact_id):
        sub = parent._build_sub_agent(system_prompt="s", tools=tool_definitions,
                                      agent_type="coder", artifact_id=artifact_id)

        async def _cf(message):
            prompts.append((artifact_id, message))
            return True   # approve

        sub.confirm_fn = _cf
        return sub

    a = make("agent-001")
    b = make("agent-002")
    msg = "rm -rf build/"

    # A confirms once, then is deduped on the repeat (same agent)
    assert asyncio.run(a._confirm_if_needed(msg)) is True
    assert asyncio.run(a._confirm_if_needed(msg)) is True
    # B must STILL be prompted (sibling approval not reused)
    assert asyncio.run(b._confirm_if_needed(msg)) is True

    a_prompts = [m for (aid, m) in prompts if aid == "agent-001"]
    b_prompts = [m for (aid, m) in prompts if aid == "agent-002"]
    assert len(a_prompts) == 1   # A prompted once (second call deduped)
    assert len(b_prompts) == 1   # B prompted despite A's prior approval
    # the messages carried each agent's identity
    assert "agent-001" in a_prompts[0]
    assert "agent-002" in b_prompts[0]


def test_confirm_if_needed_denial_not_cached():
    """A denied action must not be cached as approved (re-prompts next time)."""
    parent = _agent()
    sub = parent._build_sub_agent(system_prompt="s", tools=tool_definitions,
                                  agent_type="coder", artifact_id="agent-001")
    answers = iter([False, True])

    async def _cf(message):
        return next(answers)

    sub.confirm_fn = _cf
    assert asyncio.run(sub._confirm_if_needed("danger")) is False  # denied
    assert asyncio.run(sub._confirm_if_needed("danger")) is True   # re-prompted, approved
