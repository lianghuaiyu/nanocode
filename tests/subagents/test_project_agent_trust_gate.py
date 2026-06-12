"""P4: project-agent trust gate — project-local agent definitions are only
discovered when the workspace is trusted. User-level agents are always loaded."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanocode.agents import registry as config
from nanocode.paths import data_dir


def _write(d, name, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body)


def test_project_agent_discovered_when_trusted(tmp_path, monkeypatch):
    proj = tmp_path / ".nanocode" / "agents"
    _write(proj, "projagent", "---\nname: projagent\ndescription: project local\n---\nbody")
    monkeypatch.chdir(tmp_path)
    # trusted (the conftest autouse already patches _project_agents_trusted -> True,
    # but assert explicitly here too for clarity).
    monkeypatch.setattr(config, "_project_agents_trusted", lambda: True)
    config.reset_agent_cache()
    agents = config._discover_custom_agents()
    assert "projagent" in agents


def test_project_agent_NOT_discovered_when_untrusted(tmp_path, monkeypatch):
    proj = tmp_path / ".nanocode" / "agents"
    _write(proj, "projagent", "---\nname: projagent\ndescription: project local\n---\nbody")
    # also the legacy project dir
    proj2 = tmp_path / ".agents" / "agents"
    _write(proj2, "legacyproj", "---\nname: legacyproj\n---\nbody")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_project_agents_trusted", lambda: False)
    config.reset_agent_cache()
    agents = config._discover_custom_agents()
    assert "projagent" not in agents      # .nanocode/agents project dir gated
    assert "legacyproj" not in agents     # .agents/agents project dir gated


def test_user_agent_always_discovered_even_when_untrusted(tmp_path, monkeypatch):
    # user-level dir: data_dir()/agents (= ~/.nanocode/agents under NANOCODE_HOME)
    user_dir = data_dir() / "agents"
    _write(user_dir, "useragent", "---\nname: useragent\ndescription: mine\n---\nbody")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_project_agents_trusted", lambda: False)
    config.reset_agent_cache()
    agents = config._discover_custom_agents()
    assert "useragent" in agents  # user agents are the user's own -> always loaded


def test_build_profile_ignores_untrusted_project_agent(tmp_path, monkeypatch):
    proj = tmp_path / ".nanocode" / "agents"
    _write(proj, "projagent",
           "---\nname: projagent\nallowed-tools: read_file\n---\nReview.")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_project_agents_trusted", lambda: False)
    config.reset_agent_cache()
    # untrusted: projagent is unknown -> build_profile falls to general semantics.
    profile = config.build_profile("projagent")
    assert profile.source is None  # not loaded from the project .md


def test_reset_agent_cache_reevaluates_trust(tmp_path, monkeypatch):
    proj = tmp_path / ".nanocode" / "agents"
    _write(proj, "projagent", "---\nname: projagent\n---\nbody")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(config, "_project_agents_trusted", lambda: False)
    config.reset_agent_cache()
    assert "projagent" not in config._discover_custom_agents()

    # flip to trusted + reset -> now discovered
    monkeypatch.setattr(config, "_project_agents_trusted", lambda: True)
    config.reset_agent_cache()
    assert "projagent" in config._discover_custom_agents()


def test_project_agents_trusted_reads_is_trusted(tmp_path, monkeypatch):
    # the gate delegates to trust.is_trusted(cwd); verify both directions.
    # (test the real impl, not the conftest stub that overrides the wrapper)
    import nanocode.trust as trust
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    assert config._project_agents_trusted_impl() is True
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: False)
    assert config._project_agents_trusted_impl() is False


def test_project_agents_trusted_fail_closed_on_error(tmp_path, monkeypatch):
    import nanocode.trust as trust

    def _boom(cwd):
        raise RuntimeError("trust store unreadable")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trust, "is_trusted", _boom)
    # error -> fail-closed (untrusted), never silently load project agents.
    assert config._project_agents_trusted_impl() is False
