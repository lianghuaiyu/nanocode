from nanocode.tools import read_file, write_file, edit_file, list_files, grep_search
from nanocode.tools.context import default_tool_context

_CTX = default_tool_context()


def test_read_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("l1\nl2")
    out = read_file.run(_CTX, {"file_path": str(p)})
    assert "l1" in out and "l2" in out


def test_write_file(tmp_path):
    p = tmp_path / "b.txt"
    out = write_file.run(_CTX, {"file_path": str(p), "content": "hello"})
    assert p.read_text() == "hello"
    assert "Successfully wrote" in out


def test_edit_file(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("foo bar")
    out = edit_file.run(_CTX, {"file_path": str(p), "old_string": "foo", "new_string": "baz"})
    assert p.read_text() == "baz bar"
    assert "Successfully edited" in out


def test_edit_not_unique(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("x x")
    out = edit_file.run(_CTX, {"file_path": str(p), "old_string": "x", "new_string": "y"})
    assert "unique" in out.lower()


def test_edit_not_found(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("abc")
    out = edit_file.run(_CTX, {"file_path": str(p), "old_string": "zzz", "new_string": "y"})
    assert "not found" in out.lower()


def test_list_files(tmp_path):
    (tmp_path / "f.py").write_text("x")
    out = list_files.run(_CTX, {"path": str(tmp_path)})
    assert "f.py" in out


def test_list_files_schema_matches_pi_style_ls():
    props = list_files.SCHEMA["input_schema"]["properties"]
    assert set(props) == {"path", "limit"}
    assert list_files.SCHEMA["input_schema"]["required"] == []


def test_list_files_sorted_alphabetically_with_directory_suffix(tmp_path):
    (tmp_path / "Beta.txt").write_text("x")
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".env").write_text("x")
    out = list_files.run(_CTX, {"path": str(tmp_path)})
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines == [".env", "alpha/", "Beta.txt"]


def test_list_files_limit_reports_overflow(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("x")
    out = list_files.run(_CTX, {"path": str(tmp_path), "limit": 2})
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[:2] == ["f0.py", "f1.py"]
    assert "[2 entries limit reached. Use limit=4 for more]" in out


def test_list_files_legacy_recursive_glob_lists_prefix_only(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "src" / "pkg").mkdir()
    (tmp_path / "src" / "pkg" / "deep.py").write_text("x")
    out = list_files.run(_CTX, {"pattern": "src/**/*", "path": str(tmp_path)})
    assert "a.py" in out
    assert "pkg/" in out
    assert "deep.py" not in out



def test_grep_search(tmp_path):
    (tmp_path / "g.txt").write_text("needle here\nother")
    out = grep_search.run(_CTX, {"pattern": "needle", "path": str(tmp_path)})
    assert "needle" in out


# ─── docs/24 Phase 2 行为等价锚定（零行为变更回归）──────────────────────────


def test_write_cap_does_not_block_protected_path(tmp_path):
    """FsWriteCap 不得自行拦 protected 路径——protected 写裁决归咽喉点（confirm/deny）。

    咽喉点放行（用户确认 / bypass）后，工具应像今天一样裸写成功；把手内若再拦，会把
    已确认的 protected 写翻成 PermissionError，覆盖用户审批（真实行为变更）。"""
    target = tmp_path / ".git" / "hooks" / "evil"
    out = write_file.run(_CTX, {"file_path": str(target), "content": "x"})
    assert out.startswith("Successfully wrote")
    assert target.read_text() == "x"


def test_list_files_is_dir_propagates_oserror(tmp_path):
    """FsListCap.is_dir 须复刻 pathlib.Path.is_dir 语义：真实 stat 错误抛 OSError
    （而非 os.path.isdir 那样吞错返回 False）——这是 list_files 的 `except OSError: continue`
    仍能丢弃坏条目的前提；若吞错，坏条目会被当普通文件列出（行为变更）。"""
    import pytest
    from nanocode.tools.context import FsListCap
    from nanocode.capabilities.sandbox import FileSystemPolicy

    cap = FsListCap(FileSystemPolicy((), (), (), ()))

    class _Boom:
        def __fspath__(self):
            raise OSError("simulated stat failure")

    with pytest.raises(OSError):
        cap.is_dir(_Boom())


def test_list_files_drops_entry_on_stat_oserror(tmp_path):
    """端到端：per-entry is_dir 抛 OSError 的条目被 `except OSError: continue` 丢弃，
    而非以普通文件名列出。用一个对特定条目抛 OSError 的 cap 子类驱动 list_files。"""
    from nanocode.tools.context import FsListCap, ToolContext
    from nanocode.capabilities.sandbox import FileSystemPolicy

    (tmp_path / "good.txt").write_text("x")
    (tmp_path / "boom").write_text("y")

    class _BoomCap(FsListCap):
        def is_dir(self, path: str) -> bool:
            if str(path).endswith("boom"):
                raise OSError("simulated stat failure")
            return super().is_dir(path)

    ctx = ToolContext(fs_list=_BoomCap(FileSystemPolicy((), (), (), ())))
    out = list_files.run(ctx, {"path": str(tmp_path)})
    assert "good.txt" in out
    assert "boom" not in out


# ─── Phase 2b containment：真实(非哨兵)策略的强制路径正向/负向覆盖 ─────────────
# 这些测试不经 default_tool_context()（即不走 UNRESTRICTED 哨兵），而是直接铸真 writable_roots，
# 触达 FsWriteCap._check_writable 的强制分支，使「workspace 外写被拒 / read-only 拒所有写 /
# workspace 内写放行 / danger 不拦 / symlink·$TMPDIR 归一不误拒」回归后套件会变红。


def _write_ctx(*writable_roots):
    """以真实 writable_roots 铸一个仅含写把手的 ToolContext（不经哨兵退路）。"""
    from nanocode.tools.context import FsWriteCap, ToolContext
    from nanocode.capabilities.sandbox import FileSystemPolicy
    policy = FileSystemPolicy(
        readable_roots=(), writable_roots=tuple(writable_roots),
        denied_roots=(), protected_roots=())
    return ToolContext(fs_write=FsWriteCap(policy))


def test_write_cap_allows_inside_writable_root(tmp_path):
    """真实 writable_roots 含 tmp_path → 其内写放行。"""
    ctx = _write_ctx(tmp_path)
    target = tmp_path / "sub" / "f.txt"
    out = write_file.run(ctx, {"file_path": str(target), "content": "x"})
    assert out.startswith("Successfully wrote")
    assert target.read_text() == "x"


def test_write_cap_rejects_outside_writable_root(tmp_path):
    """真实 writable_roots = (tmp_path/inside,) → 对其外(tmp_path/outside)写被拒。

    用 tmp_path 的两个兄弟子目录，避免依赖 /etc 等系统路径（不碰系统文件）。"""
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = _write_ctx(inside)
    out = write_file.run(ctx, {"file_path": str(outside / "f.txt"), "content": "x"})
    # write_file 把把手抛的 PermissionError 转成错误文本（不落盘）。
    assert "write denied" in out or "Error" in out
    assert not (outside / "f.txt").exists()


def test_write_cap_readonly_empty_roots_rejects_all(tmp_path):
    """writable_roots=() (read-only 档语义) → 任何写被拒。"""
    ctx = _write_ctx()  # 空 roots
    out = write_file.run(ctx, {"file_path": str(tmp_path / "f.txt"), "content": "x"})
    assert "write denied" in out or "Error" in out
    assert not (tmp_path / "f.txt").exists()


def test_write_cap_tmpdir_symlink_normalized_not_misrejected(tmp_path):
    """symlink/$TMPDIR 归一锚点：writable_root 经 symlink 指向真实目录时，
    对该 symlink 下路径写不被误拒（_check_writable 双侧 realpath 归一）。"""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    # writable_root 用 symlink 路径；写目标也用 symlink 路径 → realpath 双归一后落在同一真实根。
    ctx = _write_ctx(link)
    out = write_file.run(ctx, {"file_path": str(link / "f.txt"), "content": "x"})
    assert out.startswith("Successfully wrote")
    assert (real / "f.txt").read_text() == "x"


def test_danger_full_access_policy_allows_write_outside_workspace(tmp_path):
    """critical 回归锚点：danger-full-access 真实策略(非哨兵注入)的 FsWriteCap 对工作区外写**不拦**，
    与同档 shell 经 HOST backend 在宿主裸跑、可写全盘的行为对齐。

    policy_for_profile('danger-full-access', host) 现给 writable_roots=UNRESTRICTED 哨兵 →
    FsWriteCap 跳过 containment。这里用一个 workspace 之外的 tmp 兄弟目录验证放行。"""
    from nanocode.tools.context import FsWriteCap, ToolContext
    from nanocode.capabilities.sandbox import (
        HostContext, policy_for_profile, UNRESTRICTED)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    host = HostContext(
        cwd=workspace, session_id="s", workspace_roots=(workspace,),
        temp_roots=(), interactive=True)
    policy = policy_for_profile("danger-full-access", host)
    # 契约：danger 档 fs 无 containment（哨兵），与 shell-on-host 对称。
    assert policy.filesystem.writable_roots is UNRESTRICTED
    ctx = ToolContext(fs_write=FsWriteCap(policy.filesystem))
    target = outside / "f.txt"  # workspace 之外
    out = write_file.run(ctx, {"file_path": str(target), "content": "x"})
    assert out.startswith("Successfully wrote")
    assert target.read_text() == "x"


def test_filesystem_policy_rejects_str_writable_roots():
    """类型安全 fail-loud：writable_roots 既非 UNRESTRICTED 哨兵也非全-Path 元组 → 构造即抛 TypeError，
    杜绝旧魔法字符串/混杂类型被当 roots 静默写进沙箱 profile。"""
    import pytest
    from nanocode.capabilities.sandbox import FileSystemPolicy
    with pytest.raises(TypeError):
        FileSystemPolicy(readable_roots=(), writable_roots="__not_a_root__",
                         denied_roots=(), protected_roots=())
    with pytest.raises(TypeError):
        FileSystemPolicy(readable_roots=(), writable_roots=("str_root",),
                         denied_roots=(), protected_roots=())


def test_unrestricted_sentinel_is_not_iterable_as_roots():
    """误把 UNRESTRICTED 哨兵当 roots 迭代(喂给 seatbelt/bwrap/microVM) → fail-loud TypeError，
    而非静默逐字符拆成假 root。"""
    import pytest
    from nanocode.capabilities.sandbox import UNRESTRICTED
    assert bool(UNRESTRICTED) is True  # 「无约束」是允许写的姿态(非 read-only 空元组)
    with pytest.raises(TypeError):
        list(UNRESTRICTED)
