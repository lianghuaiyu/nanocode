"""docs/19 Phase 7：sandbox profile/policy 经 runtime config，public API 不暴露 adapter。"""

import asyncio
import os
from pathlib import Path

from nanocode.agent.engine import Agent
from nanocode.capabilities.sandbox import SandboxEngine, NetworkMode
from nanocode.runtime.facade import AgentConfig, AgentRuntime


def _agent(**kw):
    return Agent(api_key="test", **kw)


# ─── HostContext 由 runtime 决定 ────────────────────────────────

def test_host_context_fields(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    a = _agent(session_id="hc")
    h = a.host_context()
    assert h.session_id == "hc"
    assert h.cwd == Path(os.path.realpath(str(tmp_path)))
    assert h.is_subagent is False and h.is_background is False and h.is_hook is False
    bg = a.host_context(background=True)
    assert bg.is_background is True and bg.interactive is False


# ─── profile → policy ───────────────────────────────────────────

def test_default_policy_is_auto_workspace_write():
    a = _agent(sandbox_profile="default")
    p = a.sandbox_policy()
    assert p.engine is SandboxEngine.AUTO
    assert p.network.mode is NetworkMode.NONE
    assert p.filesystem.writable_roots  # 非空（workspace-write）


def test_profile_selection_changes_policy():
    assert _agent(sandbox_profile="read-only").sandbox_policy().engine is SandboxEngine.NATIVE
    assert _agent(sandbox_profile="vm").sandbox_policy().engine is SandboxEngine.VM
    assert _agent(sandbox_profile="strict").sandbox_policy().vm_required is True


def test_unknown_profile_falls_back_to_default():
    a = _agent(sandbox_profile="bogus")
    assert a.sandbox_policy().engine is SandboxEngine.AUTO


# ─── AgentConfig.sandbox_profile 贯通到 Agent ───────────────────

def test_agent_config_carries_sandbox_profile():
    rt = AgentRuntime()
    th = rt.thread_start(AgentConfig(api_key="test", session_id="cfgp",
                                     permission_mode="bypassPermissions",
                                     sandbox_profile="read-only"))
    try:
        assert th.agent._sandbox_profile == "read-only"
        assert th.agent.sandbox_policy().engine is SandboxEngine.NATIVE
    finally:
        th.release_lease()


# ─── facade public API：sandbox_status / set_sandbox_profile（不暴露 adapter argv）──

def test_facade_sandbox_status_and_switch():
    rt = AgentRuntime()
    th = rt.thread_start(AgentConfig(api_key="test", session_id="fcd",
                                     permission_mode="bypassPermissions"))
    try:
        s = th.sandbox_status()
        assert s["profile"] == "default"
        assert s["engine"] == "auto"
        assert "msb" not in repr(s) and "volume" not in repr(s)   # 不泄漏 adapter 细节
        th.set_sandbox_profile("read-only")
        assert th.sandbox_status()["profile"] == "read-only"
        assert th.agent.sandbox_policy().engine is SandboxEngine.NATIVE
    finally:
        th.release_lease()


def test_facade_set_sandbox_profile_rejects_unknown():
    rt = AgentRuntime()
    th = rt.thread_start(AgentConfig(api_key="test", session_id="fcd2",
                                     permission_mode="bypassPermissions"))
    try:
        import pytest
        with pytest.raises(ValueError):
            th.set_sandbox_profile("nope")
    finally:
        th.release_lease()
