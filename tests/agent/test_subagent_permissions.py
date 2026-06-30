"""Task 4: 子 agent 继承父权限 + 工具表剔除 agent（安全关键）。

与 Claude Code / Kimi Code 对齐：子继承父 permission_mode（不再无条件
bypassPermissions）、共享 confirm_fn + _confirmed_paths（确认回流）、
共享 session_id + task_manager、is_sub_agent 工具表强制剔除 agent。
"""

import asyncio

import pytest

from nanocode.agent.engine import Agent
from nanocode.tools import REGISTRY, check_permission
from .._helpers import inject_test_services

tool_definitions = REGISTRY.schemas()


def _agent(**kw):
    _injected_agent = Agent(api_key="test", **kw)
    inject_test_services(_injected_agent)
    return _injected_agent


# ─── 权限模式继承（不再 bypass） ─────────────────────────────


@pytest.mark.parametrize("mode", ["default", "plan", "acceptEdits", "dontAsk"])
def test_sub_agent_inherits_parent_permission_mode(mode):
    parent = _agent(permission_mode=mode)
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert sub.permission_mode == mode


def test_sub_agent_not_forced_to_bypass():
    parent = _agent(permission_mode="default")
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert sub.permission_mode != "bypassPermissions"


# ─── 共享 confirm_fn / _confirmed_paths（确认回流） ───────────


def test_sub_agent_shares_confirm_fn():
    async def cf(_cmd):
        return True

    parent = _agent(permission_mode="default", confirm_fn=cf)
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert sub.confirm_fn is parent.confirm_fn is cf


def test_sub_agent_shares_confirmed_paths_reference():
    shared = {"/already/confirmed"}
    parent = _agent(permission_mode="default", confirmed_paths=shared)
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert sub._confirmed_paths is parent._confirmed_paths is shared
    assert "/already/confirmed" in sub._confirmed_paths


def test_sub_agent_confirmation_propagates_to_parent():
    parent = _agent(permission_mode="default")
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    sub._confirmed_paths.add("/child/confirmed")
    assert "/child/confirmed" in parent._confirmed_paths


# ─── 共享 session_id / task_manager ──────────────────────────


def test_sub_agent_shares_session_id():
    parent = _agent(session_id="parentsid")
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert sub.session_id == "parentsid"


def test_sub_agent_explicit_session_id_overrides():
    parent = _agent(session_id="parentsid")
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder",
        session_id="explicit99",
    )
    assert sub.session_id == "explicit99"


def test_sub_agent_shares_task_manager():
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert sub.task_manager is parent.task_manager


def test_sub_agent_is_marked_sub_agent():
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert sub.is_sub_agent is True


# ─── 工具表强制剔除 agent（子不能 spawn 孙） ──────────────────


def test_sub_agent_tools_exclude_agent():
    parent = _agent()
    # tools 故意含 agent，子 tools 也必须无 agent
    assert any(t["name"] == "agent" for t in tool_definitions)
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    assert all(t["name"] != "agent" for t in sub.tools)


# ─── 安全语义：default 父的子 agent 危险 shell 需 confirm（非 bypass allow） ─


def test_default_parent_subagent_dangerous_shell_needs_confirm():
    parent = _agent(permission_mode="default")
    sub = parent._build_sub_agent(
        system_prompt="sub", tools=tool_definitions, agent_type="coder"
    )
    result = check_permission("run_shell", {"command": "rm -rf /"}, sub.permission_mode)
    assert result["action"] == "confirm"
    assert result["action"] != "allow"
