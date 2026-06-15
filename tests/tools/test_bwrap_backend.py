"""Bwrap 后端测试：A 组纯 argv（全平台），B 组 skipif-linux 集成（实跑 bwrap）。"""

import os
import sys
from pathlib import Path

import pytest

from nanocode.tools.sandbox_backends import bwrap
from nanocode.tools.sandbox_backends.base import (
    DANGER_FULL_ACCESS,
    READ_ONLY,
    WORKSPACE_WRITE,
)
from nanocode.tools.sandbox_backends.bwrap import build_bwrap_argv
from nanocode.capabilities.sandbox import (
    FileSystemPolicy, NetworkMode, NetworkPolicy, SandboxBackend, SandboxPlan,
    protected_roots_for_workspace)


def _plan(command, cwd, *, timeout_ms=30000):
    """docs/19：workspace-write SandboxPlan（adapter 只吃 plan）。"""
    c = Path(os.path.realpath(str(cwd)))
    fs = FileSystemPolicy(readable_roots=(), writable_roots=(c,),
                          denied_roots=(), protected_roots=protected_roots_for_workspace(c))
    return SandboxPlan(backend=SandboxBackend.NATIVE, command=command, cwd=c,
                       timeout_ms=timeout_ms, filesystem=fs,
                       network=NetworkPolicy(mode=NetworkMode.NONE), session_id="s")


def _has(argv, *seq):
    """argv 中是否包含连续子序列 seq。"""
    n = len(seq)
    return any(tuple(argv[i:i + n]) == tuple(seq) for i in range(len(argv) - n + 1))


# ─── A. 纯 argv 测试（无 exec，全平台跑） ─────────────────────────────


def test_workspace_write_argv_core(tmp_path):
    real = os.path.realpath(str(tmp_path))
    argv = build_bwrap_argv(WORKSPACE_WRITE, str(tmp_path), command="echo hi")
    assert argv[0] == "bwrap"
    # 整盘只读
    assert _has(argv, "--ro-bind", "/", "/")
    # cwd 读写
    assert _has(argv, "--bind", real, real)
    # 全新可写 /tmp
    assert _has(argv, "--tmpfs", "/tmp")
    # 无网络
    assert _has(argv, "--unshare-net")
    # chdir realpath(cwd)
    assert _has(argv, "--chdir", real)
    # 末尾 /bin/sh -c <command>
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_allow_network_omits_unshare_net(tmp_path):
    argv = build_bwrap_argv(
        WORKSPACE_WRITE, str(tmp_path), command="x", allow_network=True
    )
    assert "--unshare-net" not in argv


def test_protected_roots_rebind_readonly_when_present(tmp_path):
    real = os.path.realpath(str(tmp_path))
    # 造一个真实存在的 .git 目录；.nanocode 不存在
    (tmp_path / ".git").mkdir()
    argv = build_bwrap_argv(WORKSPACE_WRITE, str(tmp_path), command="x")
    git_path = os.path.join(real, ".git")
    nano_path = os.path.join(real, ".nanocode")
    # 存在的受保护目录被 ro-bind 覆盖回只读
    assert _has(argv, "--ro-bind", git_path, git_path)
    # 不存在的不出现
    assert not _has(argv, "--ro-bind", nano_path, nano_path)


def test_read_only_argv_omits_cwd_bind(tmp_path):
    real = os.path.realpath(str(tmp_path))
    (tmp_path / ".git").mkdir()
    argv = build_bwrap_argv(READ_ONLY, str(tmp_path), command="ls")
    # read-only：只 --ro-bind / / + tmpfs，不 --bind cwd
    assert _has(argv, "--ro-bind", "/", "/")
    assert _has(argv, "--tmpfs", "/tmp")
    assert not _has(argv, "--bind", real, real)
    # read-only 也不为受保护目录单独 ro-bind（整盘已只读）
    git_path = os.path.join(real, ".git")
    assert not _has(argv, "--ro-bind", git_path, git_path)
    # 默认仍无网络
    assert _has(argv, "--unshare-net")
    assert argv[-3:] == ["/bin/sh", "-c", "ls"]


def test_argv_uses_realpath_of_symlink(tmp_path):
    target = tmp_path / "realtarget"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(str(target), str(link))
    real_target = os.path.realpath(str(target))
    argv = build_bwrap_argv(WORKSPACE_WRITE, str(link), command="x")
    # 用解析后的真实路径
    assert _has(argv, "--bind", real_target, real_target)
    assert _has(argv, "--chdir", real_target)
    # 未解析的 symlink 路径不应作为 bind 出现
    link_str = str(link)
    assert not _has(argv, "--bind", link_str, link_str)


def test_fail_closed_empty_cwd():
    with pytest.raises(ValueError):
        build_bwrap_argv(WORKSPACE_WRITE, "", command="x")


def test_fail_closed_root_cwd():
    with pytest.raises(ValueError):
        build_bwrap_argv(WORKSPACE_WRITE, "/", command="x")


def test_fail_closed_relative_cwd():
    with pytest.raises(ValueError):
        build_bwrap_argv(WORKSPACE_WRITE, "relative/path", command="x")


def test_danger_full_access_raises():
    with pytest.raises(ValueError):
        build_bwrap_argv(DANGER_FULL_ACCESS, "/tmp", command="x")


def test_unknown_posture_raises():
    with pytest.raises(ValueError):
        build_bwrap_argv("bogus-posture", "/tmp", command="x")


def test_is_available_returns_bool():
    v = bwrap.is_available()
    assert isinstance(v, bool)
    assert v == (bwrap._resolve_bwrap_bin() is not None)


# ─── A (Fix A). bwrap 钉死可信绝对路径（_resolve_bwrap_bin，不走 PATH） ──────────


def test_resolve_bwrap_bin_only_trusted_abs(monkeypatch, tmp_path):
    # 在 cwd 造一个可执行的 ./bwrap，并把 cwd 放进 PATH——_resolve_bwrap_bin 不走 PATH，
    # 绝不返回 ./bwrap（解决 shutil.which 走 PATH 被劫持的问题）。
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    resolved = bwrap._resolve_bwrap_bin()
    # 只可能是固定可信目录里的绝对路径，绝不是 ./bwrap / cwd 下的 bwrap
    assert resolved in (None,) + bwrap._TRUSTED_BWRAP
    assert resolved != str(fake)


def test_build_argv_argv0_not_cwd_bwrap(monkeypatch, tmp_path):
    # 造 ./bwrap + PATH 含 cwd：build_argv 的 argv[0] 必须不是 ./bwrap（可信绝对路径或抛错）。
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    try:
        argv = bwrap.build_argv_from_plan(_plan("echo hi", str(tmp_path)))
    except FileNotFoundError:
        return  # 可信目录无 bwrap → fail-closed，未生成 ./bwrap argv，符合预期
    assert argv[0] != "./bwrap"
    assert argv[0] != str(fake)
    assert argv[0] in bwrap._TRUSTED_BWRAP


def test_resolve_bwrap_bin_hits_trusted_dir(monkeypatch, tmp_path):
    # 把可信目录 monkeypatch 成 tmp_path 下的 bwrap，造该文件可执行 → 命中其绝对路径。
    target = tmp_path / "bwrap"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    monkeypatch.setattr(bwrap, "_TRUSTED_BWRAP", (str(target),))
    assert bwrap._resolve_bwrap_bin() == str(target)
    # build_argv 用的就是这个可信绝对路径
    argv = bwrap.build_argv_from_plan(_plan("echo hi", str(tmp_path)))
    assert argv[0] == str(target)


def test_resolve_bwrap_bin_none_when_absent(monkeypatch, tmp_path):
    # 可信目录里不存在 bwrap → None（fail-closed 上游 build_argv 会 raise）。
    monkeypatch.setattr(bwrap, "_TRUSTED_BWRAP", (str(tmp_path / "nope-bwrap"),))
    assert bwrap._resolve_bwrap_bin() is None
    with pytest.raises(FileNotFoundError):
        bwrap.build_argv_from_plan(_plan("echo hi", str(tmp_path)))


# ─── A (Fix A 旧测改). bwrap 用绝对路径 argv[0]（防 PATH 注入） ──────────────────


def test_build_argv_uses_bwrap_bin_absolute(tmp_path):
    argv = build_bwrap_argv(
        WORKSPACE_WRITE, str(tmp_path), command="x", bwrap_bin="/usr/bin/bwrap"
    )
    assert argv[0] == "/usr/bin/bwrap"


def test_run_structured_resolves_absolute_bwrap(monkeypatch, tmp_path):
    import subprocess

    target = tmp_path / "bwrap"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    monkeypatch.setattr(bwrap, "_TRUSTED_BWRAP", (str(target),))
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv

        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = bwrap.run_structured_plan(_plan("echo ok", str(tmp_path)))
    assert r["error"] is None
    assert captured["argv"][0] == str(target)


def test_run_structured_fails_closed_when_bwrap_missing(monkeypatch, tmp_path):
    import subprocess

    # 可信目录里无 bwrap → fail-closed（不裸跑宿主），即便 cwd 下有 ./bwrap。
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(bwrap, "_TRUSTED_BWRAP", (str(tmp_path / "nope-bwrap"),))
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(1))
    r = bwrap.run_structured_plan(_plan("echo ok", str(tmp_path)))
    assert r["error"] == "bwrap not found in trusted paths"
    assert called == []  # fail-closed：不裸跑


# ─── A (Fix F). 不存在的受保护目录用 --tmpfs ─────────────────────────────


def test_protected_roots_tmpfs_when_absent(tmp_path):
    real = os.path.realpath(str(tmp_path))
    # 造存在的 .git；.codex 不存在
    (tmp_path / ".git").mkdir()
    argv = build_bwrap_argv(WORKSPACE_WRITE, str(tmp_path), command="x")
    git_path = os.path.join(real, ".git")
    codex_path = os.path.join(real, ".codex")
    # 存在 → ro-bind（可读、写被拒）
    assert _has(argv, "--ro-bind", git_path, git_path)
    # 不存在 → tmpfs（空，写落 throwaway，不持久到宿主）
    assert _has(argv, "--tmpfs", codex_path)
    # 不存在的不应再以 ro-bind 出现
    assert not _has(argv, "--ro-bind", codex_path, codex_path)


# ─── B. skipif-linux 集成测试（实跑 bwrap） ─────────────────────────

_smoke = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not bwrap.is_available(),
    reason="requires Linux with bwrap",
)


@_smoke
def test_smoke_write_inside_cwd_ok(tmp_path):
    r = bwrap.run_structured_plan(_plan("echo inside > a.txt && cat a.txt", str(tmp_path)))
    assert r["error"] is None
    assert r["exit_code"] == 0, r
    f = tmp_path / "a.txt"
    assert f.exists()
    assert f.read_text().strip() == "inside"


@_smoke
def test_smoke_write_outside_cwd_denied(tmp_path):
    home = os.path.realpath(os.path.expanduser("~"))
    outside = os.path.join(home, f".nanocode_bwrap_outside_{os.getpid()}_{tmp_path.name}.txt")
    if os.path.exists(outside):
        os.unlink(outside)
    try:
        r = bwrap.run_structured_plan(_plan(f"echo x > {outside}", str(tmp_path)))
        assert r["error"] is None
        assert r["exit_code"] != 0, r
        assert not os.path.exists(outside)
    finally:
        if os.path.exists(outside):
            os.unlink(outside)


@_smoke
def test_smoke_write_dotgit_denied(tmp_path):
    (tmp_path / ".git").mkdir()
    r = bwrap.run_structured_plan(_plan("echo x > .git/HACK", str(tmp_path)))
    assert r["error"] is None
    assert r["exit_code"] != 0, r
    assert not (tmp_path / ".git" / "HACK").exists()


@_smoke
def test_smoke_network_denied(tmp_path):
    if os.environ.get("NANOCODE_HAS_NETWORK") == "0":
        pytest.skip("no network available to test denial against")
    r = bwrap.run_structured_plan(
        _plan("curl -sS --max-time 5 https://example.com", str(tmp_path), timeout_ms=15000))
    assert r["error"] is None
    assert r["exit_code"] != 0, r
