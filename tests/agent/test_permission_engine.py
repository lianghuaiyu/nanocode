"""P0.5: PermissionEngine — 单一可测决策点 + 不可绕过的 callgate 不变量。

把两条 enforcement 路径（后端 check_permission + engine allowlist）统一到一个决策对象，
并断言：无论哪条真实派发路径，都过 _execute_tool_call 这个 fail-closed 咽喉点——
新增一条绕过它的派发路径会让这些测试失败。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nanocode.agent.engine import Agent
from nanocode.tools.permissions import (
    PermissionEngine, Decision, allowlist_blocks,
    ALWAYS_ALLOWED_META, AGENT_META_TOOL,
)
from nanocode.subagents import config


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="pesid", **kw)


def _read_only_sub(parent):
    cfg = config.get_sub_agent_config("explore")
    return parent._build_sub_agent(
        system_prompt=cfg["system_prompt"], tools=cfg["tools"], agent_type="explore")


# ─── 纯函数 allowlist_blocks ────────────────────────────────────

def test_allowlist_blocks_pure():
    # 主 agent（None）→ 永不拦截
    assert allowlist_blocks("run_shell", None) is False
    # 'agent' → 子 agent 一律拦截
    assert allowlist_blocks(AGENT_META_TOOL, {"read_file"}) is True
    # 纯宿主 meta → 放行
    for name in ALWAYS_ALLOWED_META:
        assert allowlist_blocks(name, {"read_file"}) is False
    # 受约束、不在集内 → 拦截；在集内 → 放行
    assert allowlist_blocks("run_shell", {"read_file"}) is True
    assert allowlist_blocks("read_file", {"read_file"}) is False


# ─── PermissionEngine.check（单一决策入口，纯决策）──────────────

def test_engine_check_combines_policy_and_allowlist():
    # 用最小 stub agent（只需三个属性）验证 check 不依赖完整 Agent
    stub = SimpleNamespace(permission_mode="default", _plan_file_path=None,
                           _allowed_tool_names={"read_file"})
    eng = PermissionEngine(stub)
    # read_file：policy allow + 在 allowlist
    d = eng.check("read_file", {"file_path": "x"})
    assert isinstance(d, Decision)
    assert d.action == "allow" and d.allowlist_blocked is False
    # run_shell 非危险：policy allow，但不在 allowlist → allowlist_blocked True
    d2 = eng.check("run_shell", {"command": "ls"})
    assert d2.action == "allow" and d2.allowlist_blocked is True


def test_engine_check_no_side_effects():
    """check 是纯决策：不 emit、不审批、不执行。"""
    emitted = []
    stub = SimpleNamespace(permission_mode="default", _plan_file_path=None,
                           _allowed_tool_names=None,
                           tracer=SimpleNamespace(emit=lambda *a, **k: emitted.append(a)))
    eng = PermissionEngine(stub)
    eng.check("write_file", {"file_path": "/tmp/new-file-xyz", "content": "x"})
    assert emitted == []  # check 不 emit；permission_decision 由 _authorize_dispatch 负责


# ─── callgate 不变量：fail-closed、不可绕过 ─────────────────────

def test_callgate_blocks_even_when_backend_precheck_bypassed(tmp_path):
    """直接调 _execute_tool_call（= 绕过后端预检的新派发路径）仍被 allowlist 兜底拦截。"""
    parent = _agent()
    sub = _read_only_sub(parent)
    res = asyncio.run(sub._execute_tool_call("run_shell", {"command": "echo hi"}))
    assert "not permitted" in res.lower() and "run_shell" in res


def test_callgate_is_single_chokepoint_no_path_escapes(tmp_path, monkeypatch):
    """把 allowlist 闸强制全拦截后，任何真实派发路径都不得执行到真实工具/后台 shell。

    覆盖：前台真实工具、CONCURRENCY_SAFE 工具（anthropic 早执行的工具类型，早执行也是
    包 _execute_tool_call）、后台 run_shell 分支。若将来新增一条绕过 callgate 的派发路径，
    本测试会因真实工具被执行 / 后台 shell 被 spawn 而失败。
    """
    parent = _agent()
    executed = []
    spawned = []
    orig_real = parent._run_real_tool
    orig_bg = parent._spawn_background_shell

    async def _spy_real(name, inp):
        executed.append(name)
        return await orig_real(name, inp)

    async def _spy_bg(*a, **k):
        spawned.append(a)
        return await orig_bg(*a, **k)

    monkeypatch.setattr(parent, "_run_real_tool", _spy_real)
    monkeypatch.setattr(parent, "_spawn_background_shell", _spy_bg)
    # 强制 allowlist 闸全拦截（模拟「一切真实工具都不被允许」）
    monkeypatch.setattr(parent.permission, "allowlist_blocks", lambda name: True)

    # 前台真实工具
    r1 = asyncio.run(parent._execute_tool_call("write_file", {"file_path": str(tmp_path / "a"), "content": "x"}))
    assert "not permitted" in r1.lower()
    # CONCURRENCY_SAFE 工具（anthropic 早执行就是包 _execute_tool_call 跑此类工具）
    r2 = asyncio.run(parent._execute_tool_call("read_file", {"file_path": str(tmp_path / "missing")}))
    assert "not permitted" in r2.lower()
    # 后台 run_shell 分支（run_in_background 早于 meta 分流，必经 callgate）
    r3 = asyncio.run(parent._execute_tool_call("run_shell", {"command": "echo x", "run_in_background": True}))
    assert "not permitted" in r3.lower()

    assert executed == [], f"real tool dispatched past the callgate: {executed}"
    assert spawned == [], f"background shell spawned past the callgate: {spawned}"


def test_early_streaming_safe_tool_starts_then_blocked_at_callgate(tmp_path, monkeypatch):
    """anthropic 早执行不变量：policy=allow 的 CONCURRENCY_SAFE 工具会被早启动（predicate
    用 permission.check().action），但其早执行体即 _execute_tool_call，callgate 仍 fail-closed。

    证明「policy 允许 → 早启动；callgate 仍拦截」，覆盖最易被未来改动绕过的早执行路径。
    """
    from nanocode.tools import CONCURRENCY_SAFE_TOOLS
    parent = _agent()
    # 强制 allowlist 全拦截；但 policy 对只读工具仍 allow → 早执行 predicate 会放行启动
    monkeypatch.setattr(parent.permission, "allowlist_blocks", lambda name: True)
    safe = "read_file"
    assert safe in CONCURRENCY_SAFE_TOOLS
    # _on_tool_block 的早执行 predicate：permission.check(...).action == "allow"
    assert parent.permission.check(safe, {"file_path": str(tmp_path / "x")}).action == "allow"
    # 早执行体（= asyncio.create_task(_execute_tool_call(...))）仍过 callgate
    executed = []
    orig = parent._run_real_tool

    async def _spy(name, inp):
        executed.append(name)
        return await orig(name, inp)

    monkeypatch.setattr(parent, "_run_real_tool", _spy)
    res = asyncio.run(parent._execute_tool_call(safe, {"file_path": str(tmp_path / "x")}))
    assert "not permitted" in res.lower()
    assert executed == []


# ─── _authorize_dispatch（后端共用入口）── deny/confirm/allow ────

def test_authorize_dispatch_allow_and_deny(tmp_path, monkeypatch):
    parent = _agent(permission_mode="default")
    # allow：已存在文件的 read_file
    f = tmp_path / "f.txt"; f.write_text("x")
    allowed, denial = asyncio.run(parent._authorize_dispatch("read_file", {"file_path": str(f)}))
    assert allowed is True and denial is None

    # deny：plan 模式下 run_shell 被策略拒
    parent.permission_mode = "plan"
    allowed, denial = asyncio.run(parent._authorize_dispatch("run_shell", {"command": "ls"}))
    assert allowed is False and denial.startswith("Action denied:")


def test_authorize_dispatch_confirm_routes_through_confirm_fn(monkeypatch):
    parent = _agent(permission_mode="default")
    seen = []

    async def _confirm(msg):
        seen.append(msg)
        return False  # 用户拒绝

    parent.set_confirm_fn(_confirm)
    # 危险命令 → policy confirm → 经 confirm_fn；拒则返回固定文案
    allowed, denial = asyncio.run(parent._authorize_dispatch("run_shell", {"command": "rm -rf /tmp/zzz"}))
    assert allowed is False and denial == "User denied this action."
    assert seen, "confirm_fn was not consulted"
