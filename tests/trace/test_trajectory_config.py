"""trajectory config 开关：trajectory_enabled（flag + env）/ trajectory_level（env / 非法 / 显式）。"""
from nanocode.trace.config import trajectory_enabled, trajectory_level


def test_trajectory_enabled_flag_wins(monkeypatch):
    monkeypatch.delenv("NANOCODE_TRAJECTORY", raising=False)
    assert trajectory_enabled(True) is True


def test_trajectory_enabled_env(monkeypatch):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("NANOCODE_TRAJECTORY", v)
        assert trajectory_enabled() is True


def test_trajectory_disabled_default(monkeypatch):
    monkeypatch.delenv("NANOCODE_TRAJECTORY", raising=False)
    assert trajectory_enabled() is False
    monkeypatch.setenv("NANOCODE_TRAJECTORY", "off")
    assert trajectory_enabled() is False


def test_trajectory_level_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_TRAJECTORY_LEVEL", "full")
    assert trajectory_level() == "full"
    monkeypatch.setenv("NANOCODE_TRAJECTORY_LEVEL", "summary")
    assert trajectory_level() == "summary"


def test_trajectory_level_invalid_falls_back_to_summary(monkeypatch):
    monkeypatch.setenv("NANOCODE_TRAJECTORY_LEVEL", "bogus")
    assert trajectory_level() == "summary"
    monkeypatch.delenv("NANOCODE_TRAJECTORY_LEVEL", raising=False)
    assert trajectory_level() == "summary"


def test_trajectory_level_explicit_value_wins(monkeypatch):
    monkeypatch.setenv("NANOCODE_TRAJECTORY_LEVEL", "summary")
    assert trajectory_level("full") == "full"
    # 显式非法值 → summary
    assert trajectory_level("nope") == "summary"
