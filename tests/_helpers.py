"""tests/_helpers.py — docs/14 SessionLease 测试夹具。

`create()` 默认 lock=True，故 `SessionManager.create(sid)` 直接拿到 locked writer，绝大多数
既有「create→mutate」用例无需改动。这里集中两个便捷构造，供新用例与少数需显式 leased mgr 的
用例使用，并把「agent + 注入 locked mgr」收敛成一处（A3 起 Agent.__init__ 不再自建 mgr）。
"""

from __future__ import annotations

import os
from pathlib import Path

from nanocode.session.manager import SessionManager


def sandbox_bg_args(command: str, cwd, *, timeout_ms: int = 0, profile: str = "default"):
    """docs/19：构造后台 shell 经 SandboxManager 所需的 (sandbox, request, host, policy, approval)。"""
    from nanocode.capabilities.sandbox import (
        SandboxManager, ShellRequest, HostContext, ApprovalDecision, policy_for_profile)
    cwdp = Path(os.path.realpath(str(cwd)))
    sandbox = SandboxManager()
    request = ShellRequest(command=command, timeout_ms=timeout_ms, run_in_background=True)
    host = HostContext(cwd=cwdp, session_id="s", workspace_roots=(cwdp,),
                       temp_roots=(Path("/tmp"),), interactive=False, is_background=True)
    # background 不支持 escalate → approval 恒不批。
    return sandbox, request, host, policy_for_profile(profile, host), ApprovalDecision(approved=False)



def leased_manager(session_id: str, **kw) -> SessionManager:
    """一个持写锁的 SessionManager（=运行时 lease.manager 的等价物）。"""
    return SessionManager.create(session_id, lock=True, **kw)


def make_leased_agent(session_id: str, **agent_kw):
    """构造一个主 Agent 并注入一把 locked SessionManager（模拟 runtime 的 lease 注入）。

    返回 (agent, mgr)。默认 bypassPermissions + trace 关，便于纯逻辑用例。"""
    from nanocode.agent.engine import Agent
    agent_kw.setdefault("api_key", "test")
    agent_kw.setdefault("permission_mode", "bypassPermissions")
    a = Agent(session_id=session_id, **agent_kw)
    mgr = leased_manager(session_id)
    a._session_mgr = mgr
    return a, mgr
