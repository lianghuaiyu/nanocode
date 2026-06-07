import os
import pytest


@pytest.fixture(autouse=True)
def nanocode_home(tmp_path, monkeypatch):
    """每个测试用独立的 ~/.nanocode 根，隔离 sessions/memory/tool-results。"""
    home = tmp_path / "nchome"
    monkeypatch.setenv("NANOCODE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_caches():
    """清掉跨测试的模块级缓存（权限规则、激活工具、技能、子 agent）。"""
    def _reset():
        from nanocode.tools import reset_permission_cache, reset_activated_tools
        reset_permission_cache()
        reset_activated_tools()
        try:
            from nanocode.skills.discovery import reset_skill_cache
            reset_skill_cache()
        except Exception:
            pass
        try:
            from nanocode.subagents import reset_agent_cache
            reset_agent_cache()
        except Exception:
            pass
    _reset()
    yield
    _reset()
