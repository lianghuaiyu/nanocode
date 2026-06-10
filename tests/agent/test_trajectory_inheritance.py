"""trajectory wiring：Agent -> Tracer 的 trajectory 开关贯通 + 子 agent 继承。

覆盖：
- 主 agent 以 trajectory_enabled=True / level=summary 构造时，其真实 Tracer 带
  trajectory_enabled，且 trajectory_id == f"traj_{session_id}"（由 session_id 派生）。
- 经 _build_sub_agent 构造的子 agent 继承 trajectory_enabled / level，且因共享 session_id
  自动派生出**同一个** trajectory_id。
- 关闭态（默认）：trajectory_enabled=False，trajectory_id 为 None。

conftest 已给每个测试隔离的 NANOCODE_HOME(tmp)，wire 自动落该 tmp 下。
"""
from __future__ import annotations

from nanocode.agent.engine import Agent
from nanocode.trace.tracer import Tracer


def _agent(session_id, **kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    kw.setdefault("trace_enabled", False)
    return Agent(api_key="test", session_id=session_id, **kw)


def test_main_agent_tracer_carries_trajectory_id():
    sid = "trajsid1"
    a = _agent(sid, trajectory_enabled=True, trajectory_level="summary")
    assert a.trajectory_enabled is True
    assert a.trajectory_level == "summary"
    assert isinstance(a.tracer, Tracer)
    assert a.tracer.trajectory_enabled is True
    assert a.tracer.trajectory_level == "summary"
    assert a.tracer.trajectory_id == f"traj_{sid}"


def test_sub_agent_inherits_trajectory_and_same_id():
    sid = "trajsid2"
    parent = _agent(sid, trajectory_enabled=True, trajectory_level="summary")
    sub = parent._build_sub_agent(
        system_prompt="x", tools=[], agent_type="coder",
    )
    # 子 agent 继承开关与级别。
    assert sub.trajectory_enabled is True
    assert sub.trajectory_level == "summary"
    # 共享 session_id => Tracer 自动派生同一 trajectory_id。
    assert sub.session_id == sid
    assert sub.tracer.trajectory_enabled is True
    assert sub.tracer.trajectory_id == f"traj_{sid}"
    assert sub.tracer.trajectory_id == parent.tracer.trajectory_id


def test_disabled_by_default():
    a = _agent("trajsid3")
    assert a.trajectory_enabled is False
    assert a.tracer.trajectory_enabled is False
    assert a.tracer.trajectory_id is None


def test_full_level_propagates():
    sid = "trajsid4"
    parent = _agent(sid, trajectory_enabled=True, trajectory_level="full")
    sub = parent._build_sub_agent(system_prompt="x", tools=[], agent_type="coder")
    assert parent.tracer.trajectory_level == "full"
    assert sub.tracer.trajectory_level == "full"
    assert sub.tracer.trajectory_id == parent.tracer.trajectory_id
