"""子 Agent 配置：allowed-tools 既吃 YAML list 又吃逗号串。"""

from __future__ import annotations


def test_agent_allowed_tools_yaml_list(tmp_path, monkeypatch):
    from nanocode.agents import registry as config
    d = tmp_path / ".nanocode" / "agents"
    d.mkdir(parents=True)
    (d / "rev.md").write_text("---\nname: rev\nallowed-tools:\n  - read_file\n  - grep_search\n---\nprompt")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config._discover_custom_agents()["rev"]
    assert cfg["allowed_tools"] == ["read_file", "grep_search"]


def test_agent_allowed_tools_comma_string(tmp_path, monkeypatch):
    from nanocode.agents import registry as config
    d = tmp_path / ".nanocode" / "agents"
    d.mkdir(parents=True)
    (d / "rev2.md").write_text("---\nname: rev2\nallowed-tools: read_file,grep_search\n---\nprompt")
    monkeypatch.chdir(tmp_path)
    config.reset_agent_cache()
    cfg = config._discover_custom_agents()["rev2"]
    assert cfg["allowed_tools"] == ["read_file", "grep_search"]
