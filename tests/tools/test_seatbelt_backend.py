"""Seatbelt 后端测试：A 组纯字符串（全平台），B 组 skipif-darwin 冒烟（实跑 sandbox-exec）。"""

import os
import sys
from pathlib import Path

import pytest

from nanocode.tools.sandbox_backends import seatbelt
from nanocode.tools.sandbox_backends.base import (
    DANGER_FULL_ACCESS,
    READ_ONLY,
    WORKSPACE_WRITE,
)
from nanocode.tools.sandbox_backends.seatbelt import build_seatbelt_profile
from nanocode.capabilities.sandbox import (
    FileSystemPolicy, NetworkMode, NetworkPolicy, SandboxBackend, SandboxPlan,
    protected_roots_for_workspace)


def _plan(command, cwd, *, timeout_ms=30000):
    """docs/19：workspace-write SandboxPlan（adapter 只吃 plan）。"""
    c = Path(os.path.realpath(str(cwd)))
    writable = [c]
    for cand in (os.environ.get("TMPDIR"), "/tmp"):
        if cand and os.path.isdir(cand):
            r = Path(os.path.realpath(cand))
            if r not in writable:
                writable.append(r)
    fs = FileSystemPolicy(readable_roots=(), writable_roots=tuple(writable),
                          denied_roots=(), protected_roots=protected_roots_for_workspace(c))
    return SandboxPlan(backend=SandboxBackend.NATIVE, command=command, cwd=c,
                       timeout_ms=timeout_ms, filesystem=fs,
                       network=NetworkPolicy(mode=NetworkMode.NONE), session_id="s")


# ─── A. 纯字符串测试（无 exec，全平台跑） ─────────────────────────────


def test_read_only_profile_is_readonly():
    p = build_seatbelt_profile(READ_ONLY, "/whatever/ignored")
    assert "(deny default)" in p
    assert "(allow file-read*)" in p
    assert "/dev/null" in p
    # read-only 不含任何 workspace 写授权
    assert "(allow file-write*\n  (require-all" not in p
    assert "file-write* (subpath" not in p
    # 默认无网络
    assert "(allow network-outbound)" not in p
    assert "network*" not in p


def test_workspace_write_profile_has_carveouts(tmp_path):
    real = os.path.realpath(str(tmp_path))
    p = build_seatbelt_profile(WORKSPACE_WRITE, str(tmp_path))
    assert "(deny default)" in p
    assert "(allow file-write*\n  (require-all" in p
    # 针对 realpath(cwd) 的 subpath 出现在 profile 中
    assert f'(subpath "{real}")' in p
    # .git / .nanocode carve-out，subpath + literal 双重
    git_path = os.path.join(real, ".git")
    nano_path = os.path.join(real, ".nanocode")
    assert f'(require-not (subpath "{git_path}"))' in p
    assert f'(require-not (literal "{git_path}"))' in p
    assert f'(require-not (subpath "{nano_path}"))' in p
    assert f'(require-not (literal "{nano_path}"))' in p
    # 默认不含网络
    assert "(allow network-outbound)" not in p


def test_allow_network_flag(tmp_path):
    p = build_seatbelt_profile(WORKSPACE_WRITE, str(tmp_path), allow_network=True)
    assert "(allow network-outbound)" in p
    assert "(allow network-inbound)" in p


def test_cross_root_carveout_for_nested_cwd():
    """E：cwd 在 /private/tmp 下时，宽 root /private/tmp 的 allow 必须 carve 掉 cwd/.git
    （跨 root 交叉 carve），否则宽 root 会放行嵌套 cwd 的受保护目录。"""
    proj = "/private/tmp/proj_xyz"
    p = build_seatbelt_profile(
        WORKSPACE_WRITE,
        proj,
        writable_roots=(proj, "/private/tmp"),
    )
    # 整个 profile 含 cwd/.git 的 carve（来自 cwd root 自身）
    git_in_cwd = os.path.join(proj, ".git")
    assert f'(require-not (subpath "{git_in_cwd}"))' in p
    # 关键：/private/tmp 那条 allow 也 carve 掉落在它下面的 cwd/.git
    blocks = p.split("(allow file-write*")
    wide = [b for b in blocks if '(subpath "/private/tmp")' in b]
    assert wide, "expected an allow block for /private/tmp"
    assert f'(require-not (subpath "{git_in_cwd}"))' in wide[0]
    assert f'(require-not (literal "{git_in_cwd}"))' in wide[0]


def test_sbpl_string_escapes_quote_and_backslash():
    assert seatbelt._sbpl_string('a"b') == '"a\\"b"'
    assert seatbelt._sbpl_string("a\\b") == '"a\\\\b"'
    # 组合：先转义反斜杠，再转义引号
    assert seatbelt._sbpl_string('a\\"b') == '"a\\\\\\"b"'


def test_profile_escapes_special_chars_in_cwd(tmp_path):
    # 造一个名字里带双引号与反斜杠的可写根，直接走 writable_roots 注入（避免文件系统限制）
    weird = '/private/tmp/we"ird\\dir'
    p = build_seatbelt_profile(
        WORKSPACE_WRITE,
        str(tmp_path),
        writable_roots=(weird,),
        protected_roots=(),
    )
    # 原始未转义串不应出现；转义后的应出现
    assert weird not in p
    assert '(subpath "/private/tmp/we\\"ird\\\\dir")' in p


def test_profile_uses_realpath_of_symlink(tmp_path):
    target = tmp_path / "realtarget"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(str(target), str(link))
    real_target = os.path.realpath(str(target))
    p = build_seatbelt_profile(WORKSPACE_WRITE, str(link), protected_roots=())
    assert f'(subpath "{real_target}")' in p
    # 未解析的 symlink 路径不应作为 subpath 出现
    assert f'(subpath "{os.path.join(str(link))}")' not in p


def test_fail_closed_empty_cwd():
    with pytest.raises(ValueError):
        build_seatbelt_profile(WORKSPACE_WRITE, "")


def test_fail_closed_root_cwd():
    with pytest.raises(ValueError):
        build_seatbelt_profile(WORKSPACE_WRITE, "/")


def test_fail_closed_relative_cwd():
    with pytest.raises(ValueError):
        build_seatbelt_profile(WORKSPACE_WRITE, "relative/path")


def test_danger_full_access_raises():
    with pytest.raises(ValueError):
        build_seatbelt_profile(DANGER_FULL_ACCESS, "/private/tmp")


def test_unknown_posture_raises():
    with pytest.raises(ValueError):
        build_seatbelt_profile("bogus-posture", "/private/tmp")


def test_is_available_returns_bool():
    v = seatbelt.is_available()
    assert isinstance(v, bool)
    assert v == os.path.exists(seatbelt.SANDBOX_EXEC)


# ─── B. skipif-darwin 冒烟测试（实跑 sandbox-exec） ─────────────────────────

_smoke = pytest.mark.skipif(
    sys.platform != "darwin" or not seatbelt.is_available(),
    reason="requires macOS with sandbox-exec",
)


@_smoke
def test_smoke_write_inside_cwd_ok(tmp_path):
    r = seatbelt.run_structured_plan(_plan("echo inside > a.txt && cat a.txt", str(tmp_path)))
    assert r["error"] is None
    assert r["exit_code"] == 0, r
    f = tmp_path / "a.txt"
    assert f.exists()
    assert f.read_text().strip() == "inside"


@_smoke
def test_smoke_write_outside_cwd_denied(tmp_path):
    # 目标必须在所有 writable roots（cwd + $TMPDIR + /tmp）之外。tmp_path 位于
    # /private/tmp 下，故其父目录仍是可写的；改用 HOME 下一个唯一文件名作 "外部"。
    home = os.path.realpath(os.path.expanduser("~"))
    # 确保 HOME 确实不在 writable roots 内（否则本断言无意义）
    roots = seatbelt._writable_roots(os.path.realpath(str(tmp_path)))
    assert not any(home == r or home.startswith(r + os.sep) for r in roots)
    outside = os.path.join(home, f".nanocode_seatbelt_outside_{os.getpid()}_{tmp_path.name}.txt")
    if os.path.exists(outside):
        os.unlink(outside)
    try:
        r = seatbelt.run_structured_plan(_plan(f"echo x > {outside}", str(tmp_path)))
        assert r["error"] is None
        assert r["exit_code"] != 0, r
        assert not os.path.exists(outside)
    finally:
        if os.path.exists(outside):
            os.unlink(outside)


@_smoke
def test_smoke_network_denied(tmp_path):
    if os.environ.get("NANOCODE_HAS_NETWORK") == "0":
        pytest.skip("no network available to test denial against")
    r = seatbelt.run_structured_plan(
        _plan("curl -sS --max-time 5 https://example.com", str(tmp_path), timeout_ms=15000))
    assert r["error"] is None
    assert r["exit_code"] != 0, r


@_smoke
def test_smoke_write_dotgit_denied_under_tmp(tmp_path):
    """E 集成：cwd 在 /tmp 下时（宽 root /private/tmp 也是 writable），写 cwd/.git 仍被拒。"""
    (tmp_path / ".git").mkdir()
    r = seatbelt.run_structured_plan(_plan("echo x > .git/HACK", str(tmp_path)))
    assert r["error"] is None
    assert r["exit_code"] != 0, r
    assert not (tmp_path / ".git" / "HACK").exists()
