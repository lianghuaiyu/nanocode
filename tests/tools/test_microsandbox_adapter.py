"""docs/19 Phase 5：MicrosandboxAdapter（ephemeral VM）—— 从 SandboxPlan 构造 argv，无 raw dict。

不真起 VM：只测纯函数 build_run_argv（挂载/网络/镜像来自 plan）+ msb 解析的可信路径约束。
"""

import os
from pathlib import Path

from nanocode.tools.sandbox_backends import microsandbox as ms
from nanocode.capabilities.sandbox import (
    FileSystemPolicy, NetworkMode, NetworkPolicy, SandboxBackend, SandboxPlan)


def _plan(tmp_path, *, network=NetworkMode.NONE, writable=True, image="python:3.12", timeout_ms=120000,
          protected=()):
    cwd = Path(os.path.realpath(str(tmp_path)))
    fs = FileSystemPolicy(
        readable_roots=(), writable_roots=((cwd,) if writable else ()),
        denied_roots=(), protected_roots=tuple(protected))
    return SandboxPlan(
        backend=SandboxBackend.MICROVM, command="echo hi", cwd=cwd, timeout_ms=timeout_ms,
        filesystem=fs, network=NetworkPolicy(mode=network), session_id="s",
        vm_image=image, vm_name="nanocode-sbx-s")


def test_build_argv_mounts_cwd_realpath_rw(tmp_path):
    argv = ms.build_run_argv(_plan(tmp_path), "/fake/msb")
    workspace = os.path.realpath(str(tmp_path))
    assert "--volume" in argv
    assert f"{workspace}:/workspace:rw" in argv
    assert "--workdir" in argv and "/workspace" in argv
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_build_argv_network_none_adds_no_net(tmp_path):
    argv = ms.build_run_argv(_plan(tmp_path, network=NetworkMode.NONE), "/fake/msb")
    assert "--no-net" in argv


def test_build_argv_network_full_omits_no_net(tmp_path):
    argv = ms.build_run_argv(_plan(tmp_path, network=NetworkMode.FULL), "/fake/msb")
    assert "--no-net" not in argv


def test_build_argv_readonly_mounts_ro(tmp_path):
    argv = ms.build_run_argv(_plan(tmp_path, writable=False), "/fake/msb")
    workspace = os.path.realpath(str(tmp_path))
    assert f"{workspace}:/workspace:ro" in argv


def test_build_argv_protected_roots_remounted_ro(tmp_path):
    # 受保护根（存在、落在 workspace 内）在 rw workspace 之上重新 ro 覆盖（review HIGH-4）。
    (tmp_path / ".git").mkdir()
    cwd = Path(os.path.realpath(str(tmp_path)))
    protected = (Path(os.path.realpath(str(cwd / ".git"))),)
    argv = ms.build_run_argv(_plan(tmp_path, protected=protected), "/fake/msb")
    ws = os.path.realpath(str(tmp_path))
    assert f"{ws}:/workspace:rw" in argv
    assert f"{ws}/.git:/workspace/.git:ro" in argv


def test_build_argv_protected_root_outside_workspace_skipped(tmp_path):
    # .git gitdir pointer target 在 workspace 之外 → 不挂载（未挂载即 VM 内不可写）。
    outside = Path(os.path.realpath(str(tmp_path.parent / "elsewhere_gitdir")))
    argv = ms.build_run_argv(_plan(tmp_path, protected=(outside,)), "/fake/msb")
    assert not any(str(outside) in a for a in argv)


def test_build_argv_uses_plan_image(tmp_path):
    argv = ms.build_run_argv(_plan(tmp_path, image="node:22"), "/fake/msb")
    assert "node:22" in argv


def test_mount_comes_only_from_plan_cwd(tmp_path):
    # mount 只来自 plan.cwd realpath（runtime 注入），无 _cwd/_session_id 隐藏字段入口。
    plan = _plan(tmp_path)
    assert not hasattr(plan, "_cwd") and not hasattr(plan, "_session_id")
    argv = ms.build_run_argv(plan, "/fake/msb")
    workspace = os.path.realpath(str(tmp_path))
    # 唯一的 --volume 是 plan.cwd → /workspace；不存在第二个来自模型 bool 的挂载。
    assert argv.count("--volume") == 1
    assert f"{workspace}:/workspace:rw" in argv


def test_run_plan_missing_msb_returns_structured_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_resolve_msb", lambda: None)
    r = ms.run_plan(_plan(tmp_path))
    assert r["error"] and "msb" in r["error"]
    assert "run_shell" not in r["error"]            # 不提示 fall back to run_shell


# ─── msb 解析：可信路径，不走 PATH，不接受 cwd 内/非 msb 启动器 ─────────────

def test_is_trusted_msb_rejects_non_msb_basename(tmp_path):
    sh = tmp_path / "sh"; sh.write_text("#!/bin/sh\n"); sh.chmod(0o755)
    assert ms._is_trusted_msb(str(sh)) is False     # basename != msb


def test_is_trusted_msb_rejects_relative(tmp_path):
    assert ms._is_trusted_msb("msb") is False
    assert ms._is_trusted_msb("./msb") is False


def test_is_trusted_msb_rejects_inside_cwd(tmp_path, monkeypatch):
    msb = tmp_path / "msb"; msb.write_text("#!/bin/sh\n"); msb.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert ms._is_trusted_msb(str(msb)) is False     # 指向 cwd 内文件（劫持）→ 拒

def test_is_trusted_msb_accepts_valid_outside_cwd(tmp_path, monkeypatch):
    outside = tmp_path / "bin"; outside.mkdir()
    msb = outside / "msb"; msb.write_text("#!/bin/sh\n"); msb.chmod(0o755)
    work = tmp_path / "work"; work.mkdir()
    monkeypatch.chdir(work)
    assert ms._is_trusted_msb(str(msb)) is True
