"""P1: 自定义 agent manifest 扩展解析 + extends 收窄 + .agents/agents 发现。"""

from __future__ import annotations

from nanocode.subagents import config


def _write(d, name, body):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body)


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
    cfg = config.get_sub_agent_config("b")
    names = {t["name"] for t in cfg["tools"]}
    assert "read_file" in names and "list_files" in names
    assert "grep_search" not in names  # disallowed wins
    assert cfg["disallowed_names"] == {"grep_search"}


def test_disallowed_wins_over_allowed_conflict(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "c", "---\nname: c\nallowed-tools: read_file\n"
                    "disallowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("c")
    names = {t["name"] for t in cfg["tools"]}
    assert "read_file" not in names  # deny wins over allow


def test_model_and_source_stored(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "m", "---\nname: m\nmodel: claude-haiku-x\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("m")
    assert cfg["model"] == "claude-haiku-x"
    assert cfg["source"].endswith("/m.md")


def test_agent_tool_always_stripped(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    # even if author explicitly allows 'agent', it must be stripped
    _write(d, "g", "---\nname: g\nallowed-tools: read_file,agent\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("g")
    names = {t["name"] for t in cfg["tools"]}
    assert "agent" not in names


def test_no_allowlist_gives_all_tools_minus_agent(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "full", "---\nname: full\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("full")
    names = {t["name"] for t in cfg["tools"]}
    assert "agent" not in names
    assert "read_file" in names and "run_shell" in names
    # allowed_names is None when there is no allow-list constraint
    assert cfg["allowed_names"] is None


# ─── max-turns / timeout-ms parsing ─────────────────────────


def test_max_turns_and_timeout_ms_parsed(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "t", "---\nname: t\nmax-turns: 7\ntimeout-ms: 1500\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("t")
    assert cfg["max_turns"] == 7
    assert cfg["timeout_ms"] == 1500


def test_bad_int_fields_become_none(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "bad", "---\nname: bad\nmax-turns: oops\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("bad")
    assert cfg["max_turns"] is None  # never crash, just None


# ─── extends intersection (child only narrows) ──────────────


def test_extends_general_with_disallowed_loses_those_tools(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "narrowed", "---\nname: narrowed\nextends: general\n"
                          "disallowed-tools: run_shell,write_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("narrowed")
    names = {t["name"] for t in cfg["tools"]}
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
    cfg = config.get_sub_agent_config("sneaky")
    names = {t["name"] for t in cfg["tools"]}
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
    cfg = config.get_sub_agent_config("child")
    names = {t["name"] for t in cfg["tools"]}
    assert names == {"read_file", "grep_search"}  # base allow ∩, minus list_files
    # scalar model inherited from base when child does not set it
    assert cfg["model"] == "base-model"


def test_extends_child_body_overrides_else_inherits(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "pbase", "---\nname: pbase\n---\nBASE BODY")
    _write(d, "pinherit", "---\nname: pinherit\nextends: pbase\n---\n")  # empty body
    _write(d, "poverride", "---\nname: poverride\nextends: pbase\n---\nOWN BODY")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    assert config.get_sub_agent_config("pinherit")["system_prompt"].strip() == "BASE BODY"
    assert config.get_sub_agent_config("poverride")["system_prompt"].strip() == "OWN BODY"


def test_extends_cycle_is_ignored_not_crash(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "x", "---\nname: x\nextends: y\nallowed-tools: read_file\n---\nbody x")
    _write(d, "y", "---\nname: y\nextends: x\nallowed-tools: read_file\n---\nbody y")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    # must not raise; tools resolve to the self allow-list
    cfg = config.get_sub_agent_config("x")
    names = {t["name"] for t in cfg["tools"]}
    assert names == {"read_file"}


def test_extends_missing_base_is_ignored(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "orphan", "---\nname: orphan\nextends: does-not-exist\n"
                        "allowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config.get_sub_agent_config("orphan")
    names = {t["name"] for t in cfg["tools"]}
    assert names == {"read_file"}


def test_reserved_type_cannot_be_extended(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "sneaky2", "---\nname: sneaky2\nextends: memory-curator\n"
                         "allowed-tools: read_file\n---\nbody")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    # extends reserved is ignored -> falls back to self allow-list only
    cfg = config.get_sub_agent_config("sneaky2")
    names = {t["name"] for t in cfg["tools"]}
    assert names == {"read_file"}


def test_reserved_name_md_does_not_override(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "agents"
    _write(d, "memory-curator", "---\nname: memory-curator\nallowed-tools: run_shell\n---\nevil")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    # reserved type ignored at discovery; not in custom agents
    assert "memory-curator" not in config._discover_custom_agents()
    cfg = config.get_sub_agent_config("memory-curator")
    assert cfg["tools"] == []  # built-in curator config wins


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
