"""docs/19 Phase 3：SandboxManager 纯规划器决策矩阵。

native-first / VM-on-demand。用注入的 native_probe/vm_probe 钉死后端可用性，逐格验证
(policy × request × approval × availability) → SandboxPlan|SandboxDeny，不依赖宿主真实沙盒。
"""

import os
from pathlib import Path

import pytest

from nanocode.capabilities.sandbox import (
    ApprovalDecision,
    FileSystemPolicy,
    HostContext,
    NetworkMode,
    NetworkPolicy,
    SandboxBackend,
    SandboxDeny,
    SandboxEngine,
    SandboxManager,
    SandboxPlan,
    SandboxPolicy,
    ShellRequest,
    choose_backend,
    native_can_enforce,
    policy_for_profile,
    protected_roots_for_workspace,
    requires_vm,
    vm_can_enforce,
)


def _host(tmp_path, *, interactive=True, **kw):
    cwd = Path(os.path.realpath(str(tmp_path)))
    return HostContext(
        cwd=cwd, session_id="sess1", workspace_roots=(cwd,), temp_roots=(),
        interactive=interactive, **kw)


def _mgr(*, native=True, vm=True):
    return SandboxManager(native_probe=lambda: native, vm_probe=lambda: vm)


def _req(command="make build", **kw):
    return ShellRequest(command=command, timeout_ms=30000, **kw)


NO = ApprovalDecision(approved=False)
YES = ApprovalDecision(approved=True)


# ─── ShellRequest 解析 ───────────────────────────────────────────

def test_shell_request_from_tool_input():
    r = ShellRequest.from_tool_input({"command": "ls", "timeout": 5000, "escalate": True})
    assert r.command == "ls" and r.timeout_ms == 5000 and r.escalate is True
    assert r.run_in_background is False
    r2 = ShellRequest.from_tool_input({"command": "ls"})
    assert r2.timeout_ms == 30000  # 缺省 ms


# ─── default → native ───────────────────────────────────────────

def test_default_policy_plans_native(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("default", host)
    plan = _mgr(native=True, vm=True).plan_shell(_req(), host, policy, NO)
    assert isinstance(plan, SandboxPlan)
    assert plan.backend is SandboxBackend.NATIVE
    assert plan.cwd == host.cwd
    assert plan.network.mode is NetworkMode.NONE


# ─── auto + native missing + vm available + vm allowed → microVM ──

def test_auto_native_missing_vm_available_upgrades_vm(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("default", host)
    plan = _mgr(native=False, vm=True).plan_shell(_req(), host, policy, NO)
    assert isinstance(plan, SandboxPlan)
    assert plan.backend is SandboxBackend.MICROVM
    assert plan.vm_image == "python:3.12"
    assert plan.vm_name == "nanocode-sbx-sess1"


# ─── auto + native missing + vm missing → deny（绝不 host fallback）──

def test_auto_no_backend_denies_never_host(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("default", host)
    deny = _mgr(native=False, vm=False).plan_shell(_req(), host, policy, NO)
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "no_backend"
    assert deny.escalation_hint is True


# ─── engine=native + native missing → deny ──────────────────────

def test_native_profile_missing_backend_denies(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("read-only", host)
    assert policy.engine is SandboxEngine.NATIVE
    deny = _mgr(native=False, vm=True).plan_shell(_req("git status"), host, policy, NO)
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "native_unavailable"


# ─── engine=vm + msb missing → deny ─────────────────────────────

def test_vm_profile_missing_msb_denies(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("vm", host)
    assert policy.engine is SandboxEngine.VM
    deny = _mgr(native=True, vm=False).plan_shell(_req(), host, policy, NO)
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "vm_unavailable"


# ─── strict（vm_required）→ microVM 即便 native 可用 ──────────────

def test_strict_requires_vm_even_if_native(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("strict", host)
    assert requires_vm(policy) is True
    plan = _mgr(native=True, vm=True).plan_shell(_req(), host, policy, NO)
    assert isinstance(plan, SandboxPlan)
    assert plan.backend is SandboxBackend.MICROVM


def test_strict_vm_required_but_vm_missing_denies(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("strict", host)
    deny = _mgr(native=True, vm=False).plan_shell(_req(), host, policy, NO)
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "no_backend"


# ─── escalate ────────────────────────────────────────────────────

def test_escalate_noninteractive_denies(tmp_path):
    host = _host(tmp_path, interactive=False)
    policy = policy_for_profile("default", host)
    deny = _mgr().plan_shell(_req(escalate=True), host, policy, NO)
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "escalation_denied"


def test_escalate_approved_default_runs_host(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("default", host)
    plan = _mgr().plan_shell(_req(escalate=True), host, policy, YES)
    assert isinstance(plan, SandboxPlan)
    assert plan.backend is SandboxBackend.HOST


def test_escalate_approved_readonly_denied(tmp_path):
    # read-only profile 无可写根 → 不允许 escalate 逃逸宿主（即便已 approve）。
    host = _host(tmp_path)
    policy = policy_for_profile("read-only", host)
    deny = _mgr().plan_shell(_req(escalate=True), host, policy, YES)
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "host_not_allowed"


# ─── network allowlist 无法 enforce → deny ───────────────────────

def test_network_allowlist_denies(tmp_path):
    host = _host(tmp_path)
    base = policy_for_profile("default", host)
    policy = SandboxPolicy(
        engine=SandboxEngine.AUTO, filesystem=base.filesystem,
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allow_domains=("pypi.org",)),
        approval_mode="default")
    deny = _mgr(native=True, vm=True).plan_shell(_req(), host, policy, NO)
    assert isinstance(deny, SandboxDeny)
    assert deny.code == "network_unenforceable"
    assert native_can_enforce(policy) is False
    assert vm_can_enforce(policy) is False


# ─── danger-full-access engine=host ──────────────────────────────

def test_danger_full_access_needs_escalate_and_approval(tmp_path):
    host = _host(tmp_path)
    policy = policy_for_profile("danger-full-access", host)
    assert policy.engine is SandboxEngine.HOST
    # docs/19 §7.2：engine=host 仍需每条 escalate + approval。
    assert isinstance(_mgr().plan_shell(_req(), host, policy, YES), SandboxDeny)          # 无 escalate
    assert isinstance(_mgr().plan_shell(_req(escalate=True), host, policy, NO), SandboxDeny)  # 未批
    plan = _mgr().plan_shell(_req(escalate=True), host, policy, YES)
    assert isinstance(plan, SandboxPlan) and plan.backend is SandboxBackend.HOST


# ─── protected roots：.git pointer target ─────────────────────────

def test_protected_roots_resolve_gitdir_pointer(tmp_path):
    real_gitdir = tmp_path / "realgit" / "worktrees" / "wt"
    real_gitdir.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").write_text(f"gitdir: {real_gitdir}\n")
    roots = protected_roots_for_workspace(ws)
    root_strs = {str(p) for p in roots}
    assert str(Path(os.path.realpath(str(ws / ".git")))) in root_strs
    assert str(Path(os.path.realpath(str(real_gitdir)))) in root_strs
    # .nanocode 等也在
    assert str(Path(os.path.realpath(str(ws / ".nanocode")))) in root_strs


def test_protected_roots_plain_git_dir(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    roots = protected_roots_for_workspace(ws)
    assert str(Path(os.path.realpath(str(ws / ".git")))) in {str(p) for p in roots}


# ─── choose_backend 直测矩阵 ──────────────────────────────────────

def test_choose_backend_matrix(tmp_path):
    host = _host(tmp_path)
    auto = policy_for_profile("default", host)
    # auto + both available → native
    assert choose_backend(auto, _req(), NO, True, True) is SandboxBackend.NATIVE
    # auto + only vm → microvm
    assert choose_backend(auto, _req(), NO, False, True) is SandboxBackend.MICROVM
    # auto + neither → None
    assert choose_backend(auto, _req(), NO, False, False) is None
    # escalate approved → host
    assert choose_backend(auto, _req(escalate=True), YES, True, True) is SandboxBackend.HOST
    # escalate not approved → None
    assert choose_backend(auto, _req(escalate=True), NO, True, True) is None
