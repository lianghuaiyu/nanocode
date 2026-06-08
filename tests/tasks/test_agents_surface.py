"""P1: /agents surface — agent definitions catalog + instance detail rendering."""

from __future__ import annotations

from nanocode.tasks.manager import TaskManager
from nanocode.subagents import config
from nanocode.tools import tasks_tool


def _write(d, name, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body)


def test_list_agent_definitions_includes_builtins_and_custom(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "reviewer", "---\nname: reviewer\ndescription: Reviews code\n"
                          "model: my-model\nallowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    text = tasks_tool.list_agent_definitions_text()
    assert "explore" in text and "plan" in text and "general" in text
    assert "reviewer" in text
    assert "Reviews code" in text
    assert "model=my-model" in text
    assert "/reviewer.md" in text  # source path shown for custom


def test_agent_definition_detail_for_name(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "rev", "---\nname: rev\ndescription: Reviewer\nextends: general\n"
                     "disallowed-tools: run_shell\nmodel: m1\n---\nReview carefully.")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    detail = tasks_tool.agent_definition_detail_text("rev")
    assert detail is not None
    assert "Agent definition: rev" in detail
    assert "Extends: general" in detail
    assert "Model: m1" in detail
    assert "run_shell" in detail  # appears in disallowed
    assert "Disallowed tools: run_shell" in detail
    assert "Review carefully" in detail  # system-prompt preview
    assert "ADVISORY" in detail  # honest P4 note
    assert "/rev.md" in detail  # source


def test_agent_definition_detail_for_builtin():
    detail = tasks_tool.agent_definition_detail_text("explore")
    assert detail is not None
    assert "Agent definition: explore" in detail
    assert "read_file" in detail
    assert "(built-in)" in detail


def test_agent_definition_detail_returns_none_for_unknown():
    assert tasks_tool.agent_definition_detail_text("agent-001") is None
    assert tasks_tool.agent_definition_detail_text("not-a-def") is None


def test_agent_definition_detail_none_for_reserved():
    # reserved curator types are not spawnable defs -> None (so /agents show falls through)
    assert tasks_tool.agent_definition_detail_text("memory-curator") is None


def test_agents_overview_has_both_sections(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "rev", "---\nname: rev\ndescription: Reviewer\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    mgr = TaskManager()
    a = mgr.create_subagent(type="explore", description="look around")
    text = tasks_tool.agents_overview_text(mgr)
    assert "Available agent definitions:" in text
    assert "Running instances:" in text
    assert "rev" in text          # a definition
    assert a.id in text           # a running instance


def test_subagent_detail_still_works_for_instance():
    mgr = TaskManager()
    a = mgr.create_subagent(type="coder", description="do a thing",
                            model="m", provider="anthropic")
    detail = tasks_tool.subagent_detail_text(mgr, a.id)
    assert a.id in detail
    assert "do a thing" in detail
