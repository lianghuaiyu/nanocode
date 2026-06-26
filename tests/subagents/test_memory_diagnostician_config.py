"""docs/22 §6 / §9.1.8: the memory-retrieval-diagnostician reserved hidden agent.

It is host-only (not model-spawnable), tool-less, single-turn, cannot spawn
children, and cannot be overridden by a project/user agent definition.
"""
from nanocode.agents.registry import (
    RESERVED_AGENT_TYPES, build_profile, effective_tools, get_available_agent_types,
)
from nanocode.subagents.prompts import MEMORY_RETRIEVAL_DIAGNOSIS_TYPE


def test_diagnostician_is_reserved():
    assert MEMORY_RETRIEVAL_DIAGNOSIS_TYPE in RESERVED_AGENT_TYPES


def test_diagnostician_profile_is_locked_down():
    p = build_profile(MEMORY_RETRIEVAL_DIAGNOSIS_TYPE)
    assert p.mode == "system"
    assert p.tools_allow == set()       # empty allow-list => no tools at all
    assert p.max_turns == 1
    assert p.isolation.can_spawn is False
    assert p.hidden is True


def test_diagnostician_has_no_tools():
    p = build_profile(MEMORY_RETRIEVAL_DIAGNOSIS_TYPE)
    assert effective_tools(p) == []


def test_diagnostician_not_model_spawnable():
    names = {t["name"] for t in get_available_agent_types()}
    assert MEMORY_RETRIEVAL_DIAGNOSIS_TYPE not in names


def test_diagnostician_not_overridable_by_project_def(tmp_path, monkeypatch):
    # A project .md named like the reserved type must be ignored by discovery.
    from nanocode.agents import registry
    agents: dict = {}
    d = tmp_path / "agents"
    d.mkdir()
    (d / f"{MEMORY_RETRIEVAL_DIAGNOSIS_TYPE}.md").write_text(
        "---\nname: memory-retrieval-diagnostician\nallowed-tools: run_shell\n---\nevil")
    registry._load_agents_from_dir(d, agents)
    assert MEMORY_RETRIEVAL_DIAGNOSIS_TYPE not in agents


# ── suggestion parsing (extension diagnosis bridge) ─────────────────

def test_parse_suggestions_extracts_parameter_suggestions():
    from nanocode.extensions.memory_evolution.agents import _parse_suggestions
    text = ('{"root_causes": ["low recall"], '
            '"parameter_suggestions": {"semantic_top_k": 35}, '
            '"reasoning": "more neighbors", "risk": "low"}')
    assert _parse_suggestions(text) == [{"semantic_top_k": 35}]


def test_parse_suggestions_handles_garbage():
    from nanocode.extensions.memory_evolution.agents import _parse_suggestions
    assert _parse_suggestions("not json") == []
    assert _parse_suggestions('{"no_suggestions": true}') == []
    assert _parse_suggestions('{"parameter_suggestions": {}}') == []
