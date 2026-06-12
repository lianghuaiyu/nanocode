"""自定义 agent manifest 扩展解析 + extends 收窄 + .agents/agents 发现
（docs/16 #7：断言面改写为 registry.build_profile / effective_tools）。"""

from __future__ import annotations

from nanocode.agents import registry as config


def _write(d, name, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body)


def _profile_names(agent_type):
    return {t["name"] for t in config.effective_tools(config.build_profile(agent_type))}


# ─── tools / disallowed-tools / model / source 解析 ──────────


def test_tools_alias_unions_allowed_tools(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "a", "---\nname: a\nallowed-tools: read_file\ntools: grep_search\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config._discover_custom_agents()["a"]
    # union of allowed-tools and tools alias
    assert set(cfg["allowed_tools"]) == {"read_file", "grep_search"}


def test_disallowed_tools_parsed_and_subtracted(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "b", "---\nname: b\ntools: read_file,grep_search,list_files\n"
                    "disallowed-tools: grep_search\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    profile = config.build_profile("b")
    names = _profile_names("b")
    assert "read_file" in names and "list_files" in names
    assert "grep_search" not in names  # disallowed wins
    assert profile.tools_deny == {"grep_search"}


def test_disallowed_wins_over_allowed_conflict(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "c", "---\nname: c\nallowed-tools: read_file\n"
                    "disallowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    names = _profile_names("c")
    assert "read_file" not in names  # deny wins over allow


def test_model_and_source_stored(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "m", "---\nname: m\nmodel: claude-haiku-x\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    profile = config.build_profile("m")
    assert profile.model == "claude-haiku-x"
    assert profile.source.endswith("/m.md")


def test_agent_tool_always_stripped(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    # even if author explicitly allows 'agent', it must be stripped
    _write(d, "g", "---\nname: g\nallowed-tools: read_file,agent\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    names = _profile_names("g")
    assert "agent" not in names


def test_no_allowlist_gives_all_tools_minus_agent(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "full", "---\nname: full\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    profile = config.build_profile("full")
    names = _profile_names("full")
    assert "agent" not in names
    assert "read_file" in names and "run_shell" in names
    # tools_allow is None when there is no allow-list constraint
    assert profile.tools_allow is None


# ─── max-turns / timeout-ms parsing ─────────────────────────


def test_max_turns_and_timeout_ms_parsed(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "t", "---\nname: t\nmax-turns: 7\ntimeout-ms: 1500\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    profile = config.build_profile("t")
    assert profile.max_turns == 7
    assert profile.timeout_ms == 1500


def test_bad_int_fields_become_none(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "bad", "---\nname: bad\nmax-turns: oops\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    assert config.build_profile("bad").max_turns is None  # never crash, just None


# ─── extends intersection (child only narrows) ──────────────


def test_extends_general_with_disallowed_loses_those_tools(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "narrowed", "---\nname: narrowed\nextends: general\n"
                          "disallowed-tools: run_shell,write_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    names = _profile_names("narrowed")
    assert "run_shell" not in names
    assert "write_file" not in names
    assert "read_file" in names  # still has the rest of general's tools


def test_extends_explore_child_cannot_gain_a_tool_base_lacks(tmp_path, monkeypatch):
    # base 'explore' is read-only; child tries to allow run_shell — must NOT gain it.
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "sneaky", "---\nname: sneaky\nextends: explore\n"
                        "allowed-tools: run_shell,read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    names = _profile_names("sneaky")
    assert "run_shell" not in names  # base lacked it; intersection excludes it
    assert "read_file" in names      # intersection of explore ∩ {run_shell,read_file}


def test_extends_custom_base(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "base", "---\nname: base\nallowed-tools: read_file,grep_search,list_files\n"
                      "model: base-model\n---\nbase body")
    _write(d, "child", "---\nname: child\nextends: base\n"
                       "disallowed-tools: list_files\n---\nchild body")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    profile = config.build_profile("child")
    names = _profile_names("child")
    assert names == {"read_file", "grep_search"}  # base allow ∩, minus list_files
    # scalar model inherited from base when child does not set it
    assert profile.model == "base-model"


def test_extends_child_body_overrides_else_inherits(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "pbase", "---\nname: pbase\n---\nBASE BODY")
    _write(d, "pinherit", "---\nname: pinherit\nextends: pbase\n---\n")  # empty body
    _write(d, "poverride", "---\nname: poverride\nextends: pbase\n---\nOWN BODY")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    assert config.build_profile("pinherit").prompt.strip() == "BASE BODY"
    assert config.build_profile("poverride").prompt.strip() == "OWN BODY"


def test_extends_cycle_is_ignored_not_crash(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "x", "---\nname: x\nextends: y\nallowed-tools: read_file\n---\nbody x")
    _write(d, "y", "---\nname: y\nextends: x\nallowed-tools: read_file\n---\nbody y")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    # must not raise; tools resolve to the self allow-list
    names = _profile_names("x")
    assert names == {"read_file"}


def test_extends_missing_base_is_ignored(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "orphan", "---\nname: orphan\nextends: does-not-exist\n"
                        "allowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    names = _profile_names("orphan")
    assert names == {"read_file"}


def test_reserved_type_cannot_be_extended(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "sneaky2", "---\nname: sneaky2\nextends: memory-curator\n"
                         "allowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    # extends reserved is ignored -> falls back to self allow-list only
    names = _profile_names("sneaky2")
    assert names == {"read_file"}


def test_reserved_name_md_does_not_override(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "memory-curator", "---\nname: memory-curator\nallowed-tools: run_shell\n---\nevil")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    # reserved type ignored at discovery; not in custom agents
    assert "memory-curator" not in config._discover_custom_agents()
    profile = config.build_profile("memory-curator")
    assert config.effective_tools(profile) == []  # built-in curator profile wins


# ─── .agents/agents discovery + precedence ──────────────────


def test_dot_agents_agents_discovery(tmp_path, monkeypatch):
    d = tmp_path / ".agents" / "agents"
    _write(d, "fromdot", "---\nname: fromdot\nallowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    assert "fromdot" in config._discover_custom_agents()


def test_nanocode_agents_overrides_dot_agents(tmp_path, monkeypatch):
    # same name in both project dirs: .nanocode/agents (higher) must win.
    dot = tmp_path / ".agents" / "agents"
    nano = tmp_path / ".nanocode" / "agents"
    _write(dot, "dup", "---\nname: dup\ndescription: from-dot\n---\nbody")
    _write(nano, "dup", "---\nname: dup\ndescription: from-nanocode\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config._discover_custom_agents()["dup"]
    assert cfg["description"] == "from-nanocode"
    assert cfg["source"].endswith(".nanocode/agents/dup.md")


def test_user_home_dot_agents_discovery(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = home / ".agents" / "agents"
    _write(d, "userdot", "---\nname: userdot\nallowed-tools: read_file\n---\nbody")
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    assert "userdot" in config._discover_custom_agents()
