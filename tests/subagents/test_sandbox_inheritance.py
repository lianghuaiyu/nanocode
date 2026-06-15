"""docs/19 Phase 8：subagent 继承父 sandbox profile（收窄不放宽）+ hook/background 收窄。"""

import os
from pathlib import Path

from nanocode.agent.engine import Agent
from nanocode.capabilities.sandbox import (
    ApprovalDecision, HostContext, SandboxBackend, SandboxDeny, SandboxEngine, SandboxManager,
    ShellRequest, narrow_policy_for_context, policy_for_profile)


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="par", **kw)


def _host(tmp_path, **kw):
    cwd = Path(os.path.realpath(str(tmp_path)))
    return HostContext(cwd=cwd, session_id="s", workspace_roots=(cwd,),
                       temp_roots=(), interactive=True, **kw)


# ─── subagent 继承 profile ───────────────────────────────────────

def test_subagent_inherits_parent_profile():
    parent = _agent(sandbox_profile="strict")
    sub = parent._build_sub_agent(system_prompt="x", tools=[{"name": "run_shell"}],
                                  agent_type="coder")
    assert sub._sandbox_profile == "strict"
    assert sub.sandbox_policy().engine is SandboxEngine.AUTO
    assert sub.sandbox_policy().vm_required is True


def test_subagent_default_profile_inherited():
    parent = _agent(sandbox_profile="read-only")
    sub = parent._build_sub_agent(system_prompt="x", tools=[{"name": "read_file"}],
                                  agent_type="explore")
    assert sub._sandbox_profile == "read-only"
    assert sub.sandbox_policy().engine is SandboxEngine.NATIVE


# ─── hook / background 收窄：engine=HOST → AUTO（绝不裸跑宿主）─────────────

def test_narrow_hook_host_to_auto(tmp_path):
    danger = policy_for_profile("danger-full-access", _host(tmp_path))
    assert danger.engine is SandboxEngine.HOST
    narrowed = narrow_policy_for_context(danger, _host(tmp_path, is_hook=True))
    assert narrowed.engine is SandboxEngine.AUTO


def test_narrow_background_host_to_auto(tmp_path):
    danger = policy_for_profile("danger-full-access", _host(tmp_path))
    narrowed = narrow_policy_for_context(danger, _host(tmp_path, is_background=True))
    assert narrowed.engine is SandboxEngine.AUTO


def test_narrow_foreground_unchanged(tmp_path):
    p = policy_for_profile("default", _host(tmp_path))
    assert narrow_policy_for_context(p, _host(tmp_path)) is p


# ─── subagent 不能放宽：read-only subagent 的 escalate 被拒 ──────────────────

def test_readonly_subagent_cannot_escalate_to_host(tmp_path):
    host = _host(tmp_path, is_subagent=True)
    policy = policy_for_profile("read-only", host)
    mgr = SandboxManager(native_probe=lambda: True, vm_probe=lambda: True)
    deny = mgr.plan_shell(ShellRequest("rm -rf x", 30000, escalate=True), host, policy,
                          ApprovalDecision(approved=True))
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "host_not_allowed"


def test_subagent_network_stays_none_under_default(tmp_path):
    # 默认 profile 子 agent：network none（不能借子 agent 打开父未允许的网络）。
    parent = _agent(sandbox_profile="default")
    sub = parent._build_sub_agent(system_prompt="x", tools=[{"name": "run_shell"}],
                                  agent_type="coder")
    assert sub.sandbox_policy().network.mode.value == "none"
