"""docs/19 Phase 4/9：SandboxManager 执行策略（native-first / VM-on-demand / no host fallback）。

用注入的 fake backend 验证后端选择与编排；用真实 seatbelt（本机可用时）验证 confinement。
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from nanocode.capabilities.sandbox import (
    ApprovalDecision, HostContext, SandboxManager, ShellRequest, policy_for_profile)
from nanocode.tools.sandbox_backends import resolve_native_backend


class _FakeNative:
    def __init__(self):
        self.calls = []

    def run_structured_plan(self, plan):
        self.calls.append(plan)
        return {"exit_code": 0, "stdout": "NATIVE_OK", "stderr": "", "timed_out": False, "error": None}

    def build_argv_from_plan(self, plan):
        self.calls.append(plan)
        return [sys.executable, "-c", "import sys; sys.stdout.write('NATIVE_OK')"]


class _PassthroughNative:
    def build_argv_from_plan(self, plan):
        return ["/bin/sh", "-c", plan.command]


class _FakeVM:
    def __init__(self, available=True):
        self._a = available
        self.calls = []

    def is_available(self):
        return self._a

    def run_plan(self, plan):
        self.calls.append(plan)
        return {"exit_code": 0, "stdout": "VM_OK", "stderr": "", "timed_out": False, "error": None}


def _host(tmp_path, **kw):
    cwd = Path(os.path.realpath(str(tmp_path)))
    return HostContext(cwd=cwd, session_id="s", workspace_roots=(cwd,),
                       temp_roots=(Path("/tmp"),), interactive=True, **kw)


def _run(mgr, request, host, policy, approval=None):
    import asyncio
    from nanocode.capabilities.sandbox import ApprovalDecision
    # 镜像 engine：escalate 抵达执行 ⟹ 已批准（可显式覆盖）。
    if approval is None:
        approval = ApprovalDecision(approved=request.escalate)
    return asyncio.run(mgr.execute_shell(request, host, policy, approval))


def _process_table() -> str:
    return subprocess.run(
        ["ps", "-axo", "command"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


# ─── 后端选择 ────────────────────────────────────────────────────

def test_default_runs_native(tmp_path):
    native = _FakeNative()
    mgr = SandboxManager(native_backend=native, vm_adapter=_FakeVM(), native_probe=lambda: True)
    host = _host(tmp_path)
    out = _run(mgr, ShellRequest("make", 30000), host, policy_for_profile("default", host))
    assert out == "NATIVE_OK"
    assert len(native.calls) == 1
    # workspace-write：可写根含 cwd；protected 含 .git 等
    plan = native.calls[0]
    assert host.cwd in plan.filesystem.writable_roots


def test_native_missing_upgrades_vm(tmp_path):
    vm = _FakeVM()
    mgr = SandboxManager(native_backend=None, vm_adapter=vm,
                         native_probe=lambda: False, vm_probe=lambda: True)
    host = _host(tmp_path)
    out = _run(mgr, ShellRequest("make", 30000), host, policy_for_profile("default", host))
    assert out == "VM_OK"
    assert len(vm.calls) == 1


def test_no_backend_denies_with_escalate_hint(tmp_path):
    mgr = SandboxManager(native_probe=lambda: False, vm_probe=lambda: False)
    host = _host(tmp_path)
    out = _run(mgr, ShellRequest("make", 30000), host, policy_for_profile("default", host))
    assert out.startswith("[sandbox]")
    assert "escalate=true" in out


def test_strict_uses_vm(tmp_path):
    native, vm = _FakeNative(), _FakeVM()
    mgr = SandboxManager(native_backend=native, vm_adapter=vm,
                         native_probe=lambda: True, vm_probe=lambda: True)
    host = _host(tmp_path)
    out = _run(mgr, ShellRequest("make", 30000), host, policy_for_profile("strict", host))
    assert out == "VM_OK"
    assert native.calls == []  # vm_required → 跳过 native


def test_read_only_posture_has_no_writable_roots(tmp_path):
    native = _FakeNative()
    mgr = SandboxManager(native_backend=native, native_probe=lambda: True)
    host = _host(tmp_path)
    _run(mgr, ShellRequest("git status", 30000), host, policy_for_profile("read-only", host))
    assert native.calls[0].filesystem.writable_roots == ()


def test_native_mechanism_failure_emits_escalate_hint(tmp_path):
    class _Broken(_FakeNative):
        def build_argv_from_plan(self, plan):
            raise RuntimeError("sandbox-exec crashed")
    mgr = SandboxManager(native_backend=_Broken(), native_probe=lambda: True)
    host = _host(tmp_path)
    out = _run(mgr, ShellRequest("make", 30000), host, policy_for_profile("default", host))
    assert "escalate=true" in out and "failed to run" in out


def test_native_command_failure_prefixes_hint(tmp_path):
    class _Fail(_FakeNative):
        def build_argv_from_plan(self, plan):
            self.calls.append(plan)
            return [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(2)"]
    mgr = SandboxManager(native_backend=_Fail(), native_probe=lambda: True)
    host = _host(tmp_path)
    out = _run(mgr, ShellRequest("make", 30000), host, policy_for_profile("default", host))
    assert "Command failed (exit code 2)" in out
    assert out.startswith("[sandbox]")  # native fail hint 前置


# ─── escalate → host（escalate 抵达执行 ⟹ permission 已批）─────────────────

def test_escalate_runs_on_host_not_sandbox(tmp_path):
    native = _FakeNative()
    mgr = SandboxManager(native_backend=native, native_probe=lambda: True)
    host = _host(tmp_path)
    out = _run(mgr, ShellRequest("echo HOSTRAN", 30000, escalate=True),
               host, policy_for_profile("default", host))
    assert out.strip() == "HOSTRAN"          # 真宿主执行
    assert native.calls == []                # 未进 native 沙盒


def test_danger_profile_requires_escalate_per_command(tmp_path):
    # docs/19 §7.2：danger-full-access (engine=host) 仍需每条命令 escalate + approval（profile 非 blanket）。
    native = _FakeNative()
    mgr = SandboxManager(native_backend=native, native_probe=lambda: True)
    host = _host(tmp_path)
    policy = policy_for_profile("danger-full-access", host)
    # 无 escalate → deny（不静默上宿主）。
    out = _run(mgr, ShellRequest("echo DANGER", 30000), host, policy)
    assert out.startswith("[sandbox]") and native.calls == []
    # escalate + approval → host。
    out2 = _run(mgr, ShellRequest("echo DANGER", 30000, escalate=True), host, policy)
    assert out2.strip() == "DANGER" and native.calls == []


def test_host_foreground_shell_does_not_block_event_loop(tmp_path):
    async def scenario():
        mgr = SandboxManager(native_probe=lambda: False, vm_probe=lambda: False)
        host = _host(tmp_path)
        policy = policy_for_profile("danger-full-access", host)
        ticks = 0
        done = False

        async def ticker():
            nonlocal ticks
            while not done:
                ticks += 1
                await asyncio.sleep(0.02)

        ticker_task = asyncio.create_task(ticker())
        try:
            out = await mgr.execute_shell(
                ShellRequest(
                    "python3 -c 'import time; time.sleep(0.2); print(\"ASYNC_HOST_OK\")'",
                    2000,
                    escalate=True,
                ),
                host,
                policy,
                approval=ApprovalDecision(approved=True),
            )
        finally:
            done = True
            await ticker_task
        return out, ticks

    out, ticks = asyncio.run(scenario())
    assert out.strip() == "ASYNC_HOST_OK"
    assert ticks >= 3


def test_native_foreground_shell_cancel_kills_process_group(tmp_path):
    marker = f"NANOCODE_NATIVE_CANCEL_{uuid.uuid4().hex}"
    command = f"{sys.executable} -c 'import time; time.sleep(60)' {marker}"

    async def scenario():
        mgr = SandboxManager(
            native_backend=_PassthroughNative(),
            native_probe=lambda: True,
            vm_probe=lambda: False,
        )
        host = _host(tmp_path)
        policy = policy_for_profile("default", host)
        task = asyncio.create_task(mgr.execute_shell(
            ShellRequest(command, 60000),
            host,
            policy,
            approval=ApprovalDecision(approved=False),
        ))
        try:
            for _ in range(100):
                if marker in _process_table():
                    break
                await asyncio.sleep(0.02)
            assert marker in _process_table()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(100):
                if marker not in _process_table():
                    break
                await asyncio.sleep(0.02)
            return marker in _process_table()
        finally:
            if marker in _process_table():
                subprocess.run(["pkill", "-f", marker], check=False)

    assert asyncio.run(scenario()) is False


# ─── 真实 seatbelt confinement（本机可用时）─────────────────────────────────

_native = pytest.mark.skipif(resolve_native_backend() is None,
                             reason="requires a native OS sandbox backend")


@_native
def test_real_native_confines_writes(tmp_path):
    mgr = SandboxManager()
    host = _host(tmp_path)
    policy = policy_for_profile("default", host)
    # workspace 内可写
    out = _run(mgr, ShellRequest("touch inside.txt && echo ok", 30000), host, policy)
    assert out.strip() == "ok"
    assert (tmp_path / "inside.txt").exists()
    # workspace 外被拒
    outside = Path(os.path.realpath(os.path.expanduser("~"))) / f".nc_escape_{os.getpid()}.txt"
    roots = [str(r) for r in policy.filesystem.writable_roots]
    if any(str(outside).startswith(r + os.sep) for r in roots):
        pytest.skip("HOME inside writable roots")
    _run(mgr, ShellRequest(f"echo x > {outside}", 30000), host, policy)
    assert not outside.exists()


@_native
def test_real_native_protects_git(tmp_path):
    (tmp_path / ".git").mkdir()
    mgr = SandboxManager()
    host = _host(tmp_path)
    _run(mgr, ShellRequest("echo x > .git/HOOKX", 30000), host, policy_for_profile("default", host))
    assert not (tmp_path / ".git" / "HOOKX").exists()
