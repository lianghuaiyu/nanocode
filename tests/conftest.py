import os
import pytest


@pytest.fixture(autouse=True)
def nanocode_home(tmp_path, monkeypatch):
    """每个测试用独立的 ~/.nanocode 根，隔离 sessions/memory/tool-results。"""
    home = tmp_path / "nchome"
    monkeypatch.setenv("NANOCODE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _trust_cwd(monkeypatch):
    """默认信任任意工作区，使依赖项目级 .nanocode/agents 发现的既有测试照常工作。

    P4 给项目级 agent 发现加了 trust 闸（未信任则不加载项目本地 agent 定义）。生产里
    交互运行会提示并记录信任、非交互隐式信任，所以"已信任"是常态。这里把项目 agent 的
    trust 闸 _project_agents_trusted 默认打成 True（覆盖测试 chdir 后的临时目录，无需
    逐测试落 trust.json，也不动 trust.is_trusted 本体——后者的单测才能照常验证）。
    需要验证未信任发现行为的测试，再用自己的 monkeypatch.setattr 覆写为返回 False。"""
    monkeypatch.setattr(
        "nanocode.subagents.config._project_agents_trusted",
        lambda: True, raising=False,
    )
    yield


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
