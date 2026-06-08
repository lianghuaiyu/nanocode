"""P4: [agents] fleet config in .nanocode/settings.json (parse + defaults + merge)."""

from __future__ import annotations

import json

from nanocode.tools import permissions, load_agents_config
from nanocode.paths import data_dir, project_config_dir


def _write_settings(directory, obj):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "settings.json").write_text(json.dumps(obj))


def test_defaults_when_no_settings():
    permissions.reset_permission_cache()
    cfg = load_agents_config()
    assert cfg["max_threads"] == permissions.AGENTS_CONFIG_DEFAULTS["max_threads"]
    assert cfg["max_depth"] == permissions.AGENTS_CONFIG_DEFAULTS["max_depth"]
    assert cfg["default_timeout_ms"] is None
    assert cfg["background_timeout_ms"] is None


def test_user_settings_parsed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_settings(data_dir(), {"agents": {
        "max_threads": 7, "max_depth": 3,
        "default_timeout_ms": 1234, "background_timeout_ms": 5678,
    }})
    permissions.reset_permission_cache()
    cfg = load_agents_config()
    assert cfg["max_threads"] == 7
    assert cfg["max_depth"] == 3
    assert cfg["default_timeout_ms"] == 1234
    assert cfg["background_timeout_ms"] == 5678


def test_project_overrides_user_per_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_settings(data_dir(), {"agents": {"max_threads": 2, "max_depth": 9}})
    _write_settings(project_config_dir(), {"agents": {"max_threads": 5}})
    permissions.reset_permission_cache()
    cfg = load_agents_config()
    # project wins for max_threads; user value survives for unspecified max_depth
    assert cfg["max_threads"] == 5
    assert cfg["max_depth"] == 9


def test_invalid_int_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_settings(data_dir(), {"agents": {"max_threads": "not-an-int"}})
    permissions.reset_permission_cache()
    cfg = load_agents_config()
    assert cfg["max_threads"] == permissions.AGENTS_CONFIG_DEFAULTS["max_threads"]


def test_missing_agents_section_uses_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_settings(data_dir(), {"permissions": {"allow": ["read_file"]}})
    permissions.reset_permission_cache()
    cfg = load_agents_config()
    assert cfg["max_threads"] == permissions.AGENTS_CONFIG_DEFAULTS["max_threads"]


def test_cache_reset_picks_up_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_settings(data_dir(), {"agents": {"max_threads": 1}})
    permissions.reset_permission_cache()
    assert load_agents_config()["max_threads"] == 1
    # without reset, cached
    _write_settings(data_dir(), {"agents": {"max_threads": 8}})
    assert load_agents_config()["max_threads"] == 1
    permissions.reset_permission_cache()
    assert load_agents_config()["max_threads"] == 8


def test_non_dict_agents_section_ignored(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_settings(data_dir(), {"agents": "garbage"})
    permissions.reset_permission_cache()
    cfg = load_agents_config()
    assert cfg["max_threads"] == permissions.AGENTS_CONFIG_DEFAULTS["max_threads"]
