"""trajectory wiring：Agent 的 trajectory 开关（plain attrs）+ 子 agent 继承。

docs/14 Milestone B：Tracer/wire 已退役——`trajectory_enabled`/`trajectory_level` 是 Agent 的
普通属性（仅 gate `nanocode trajectory export`，不再流入任何 tracer）。trajectory_id 是**导出期**
概念（`traj_{session_id}`，由 trajectory 导出层从 session_id 派生），不再由运行时对象承载。
"""
from __future__ import annotations

from nanocode.agent.engine import Agent
from .._helpers import inject_test_services


def _agent(session_id, **kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    _injected_agent = Agent(api_key="test", session_id=session_id, **kw)
    inject_test_services(_injected_agent)
    return _injected_agent


def test_main_agent_carries_trajectory_flags():
    a = _agent("trajsid1", trajectory_enabled=True, trajectory_level="summary")
    assert a.trajectory_enabled is True
    assert a.trajectory_level == "summary"


def test_sub_agent_inherits_trajectory_flags():
    parent = _agent("trajsid2", trajectory_enabled=True, trajectory_level="summary")
    sub = parent._build_sub_agent(system_prompt="x", tools=[], agent_type="coder")
    assert sub.trajectory_enabled is True
    assert sub.trajectory_level == "summary"
    assert sub.session_id == "trajsid2"


def test_disabled_by_default():
    a = _agent("trajsid3")
    assert a.trajectory_enabled is False


def test_full_level_propagates():
    parent = _agent("trajsid4", trajectory_enabled=True, trajectory_level="full")
    sub = parent._build_sub_agent(system_prompt="x", tools=[], agent_type="coder")
    assert parent.trajectory_level == "full"
    assert sub.trajectory_level == "full"
