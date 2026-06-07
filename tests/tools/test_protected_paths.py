"""受保护项目元数据目录（.git/.nanocode/.claude/.codex/.agents）写入 → confirm/deny。

与 sandbox 的 DEFAULT_PROTECTED_ROOTS 对齐：seatbelt 下 shell 写不进这些目录，
write_file/edit_file 也应至少需要确认（confirm 既挡住又可放行；dontAsk 下映射为 deny）。
"""

import os

from nanocode.tools import check_permission
from nanocode.tools import permissions


# ─── check_permission：protected 写入 → confirm ──────────────────────────


def test_write_file_into_git_dir_confirms(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / ".git" / "hooks" / "pre-commit")
    r = check_permission("write_file", {"file_path": fp}, "default")
    assert r["action"] == "confirm"
    assert "protected" in r["message"]


def test_write_file_into_nanocode_dir_confirms(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / ".nanocode" / "config.json")
    r = check_permission("write_file", {"file_path": fp}, "default")
    assert r["action"] == "confirm"
    assert "protected" in r["message"]


def test_edit_file_into_claude_dir_confirms(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # 即使文件存在，编辑受保护目录仍需确认。
    cdir = tmp_path / ".claude"
    cdir.mkdir()
    fp = cdir / "settings.json"
    fp.write_text("{}")
    r = check_permission("edit_file", {"file_path": str(fp)}, "default")
    assert r["action"] == "confirm"
    assert "protected" in r["message"]


def test_protected_dir_root_itself_confirms(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / ".codex")
    r = check_permission("write_file", {"file_path": fp}, "default")
    assert r["action"] == "confirm"
    assert "protected" in r["message"]


# ─── 普通路径不因 protected 而 confirm ──────────────────────────────────


def test_existing_normal_file_allows(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    fp = src / "foo.py"
    fp.write_text("x = 1\n")
    r = check_permission("write_file", {"file_path": str(fp)}, "default")
    assert r["action"] == "allow"


def test_new_normal_file_confirms_but_not_protected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / "docs" / "x.md")
    r = check_permission("write_file", {"file_path": fp}, "default")
    # 不存在的新文件走既有「写新文件」confirm，但 message 不是 protected。
    assert r["action"] == "confirm"
    assert "protected" not in r["message"]
    assert "write new file" in r["message"]


# ─── dontAsk：protected 写入 → deny ──────────────────────────────────────


def test_protected_write_denied_in_dontask(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / ".git" / "config")
    r = check_permission("write_file", {"file_path": fp}, "dontAsk")
    assert r["action"] == "deny"
    assert "protected" in r["message"]


# ─── bypassPermissions：protected 是「bypass 越不过」的硬边界（D）────────────


def test_protected_write_confirms_under_bypass(monkeypatch, tmp_path):
    """D：受保护目录写确认提到 bypass 早返回之前 → bypass 下仍 confirm（不直接 allow）。"""
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / ".git" / "hooks" / "x")
    r = check_permission("write_file", {"file_path": fp}, "bypassPermissions")
    assert r["action"] == "confirm"
    assert "protected" in r["message"]


def test_protected_write_denied_under_dontask_explicit(monkeypatch, tmp_path):
    """D：protected 检查在 dontAsk 下 → deny（边界先于 bypass/acceptEdits 裁决）。"""
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / ".git" / "hooks" / "x")
    r = check_permission("write_file", {"file_path": fp}, "dontAsk")
    assert r["action"] == "deny"
    assert "protected" in r["message"]


def test_normal_path_still_allowed_under_bypass(monkeypatch, tmp_path):
    """D：普通路径在 bypass 下仍 allow（只有 protected 被提到 bypass 之前）。"""
    monkeypatch.chdir(tmp_path)
    fp = str(tmp_path / "src" / "foo.py")
    r = check_permission("write_file", {"file_path": fp}, "bypassPermissions")
    assert r["action"] == "allow"


# ─── _is_protected_path 纯函数 ───────────────────────────────────────────


def test_is_protected_path_inside(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert permissions._is_protected_path(str(tmp_path / ".git" / "x")) is True
    assert permissions._is_protected_path(str(tmp_path / ".claude" / "a" / "b")) is True
    assert permissions._is_protected_path(str(tmp_path / ".agents")) is True


def test_is_protected_path_outside(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert permissions._is_protected_path(str(tmp_path / "src" / "foo.py")) is False
    assert permissions._is_protected_path(str(tmp_path / "docs" / "x.md")) is False
    assert permissions._is_protected_path("") is False
    # cwd 外的 .git（别的策略管，不在此拦）。
    other = tmp_path.parent / "other_repo_xyz"
    other.mkdir(exist_ok=True)
    assert permissions._is_protected_path(str(other / ".git" / "config")) is False


def test_is_protected_path_no_substring_false_positive(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # ".github" 不应被 ".git" 前缀误判（用 os.sep 边界比较）。
    assert permissions._is_protected_path(str(tmp_path / ".github" / "workflows" / "ci.yml")) is False
    assert permissions._is_protected_path(str(tmp_path / ".gitignore")) is False


def test_is_protected_path_symlink(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    target = gitdir / "real.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    os.symlink(target, link)
    # symlink 指向 .git 内的文件，realpath 解析后应判为 protected。
    assert permissions._is_protected_path(str(link)) is True


# ─── D 残留：protected 锚到 git 项目根（不只 cwd）──────────────────────────────


def test_protected_path_anchored_at_git_root_from_subdir(monkeypatch, tmp_path):
    """从 repo/src 启动，repo/.git/hooks/x（项目根下）应被保护，即便不在 cwd 下。"""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    src = repo / "src"
    src.mkdir()
    monkeypatch.chdir(src)
    fp = str(repo / ".git" / "hooks" / "x")
    assert permissions._is_protected_path(fp) is True
    # 即便 bypassPermissions：protected 是 bypass 越不过的硬边界 → confirm（非 allow）。
    r = check_permission("write_file", {"file_path": fp}, "bypassPermissions")
    assert r["action"] == "confirm"
    assert "protected" in r["message"]


def test_project_root_walks_up_to_git(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "a" / "b" / "c"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert permissions._project_root() == os.path.realpath(str(repo))


def test_project_root_falls_back_to_cwd_without_git(monkeypatch, tmp_path):
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    monkeypatch.chdir(nogit)
    # 无 .git → 回退 cwd（既有用例行为不变）。
    assert permissions._project_root() == os.path.realpath(str(nogit))


# ─── D 残留 worktree/submodule：`.git` 为文件（gitfile）也算项目根 ────────────────


def test_project_root_gitfile_anchors_worktree(monkeypatch, tmp_path):
    """git worktree/submodule 的 `.git` 是文件（内含 `gitdir: ...`），不是目录。
    用 os.path.exists（而非 isdir）才能锚到项目根，否则这类 repo 回退、`../.git` 不受保护。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    gitfile = repo / ".git"
    gitfile.write_text("gitdir: /some/where/.git/worktrees/repo\n")  # gitfile，非目录
    src = repo / "src"
    src.mkdir()
    monkeypatch.chdir(src)
    assert permissions._project_root() == os.path.realpath(str(repo))


def test_protected_path_anchored_at_gitfile_root_from_subdir(monkeypatch, tmp_path):
    """gitfile worktree：从 repo/src 启动，repo/.git/hooks/x 仍受保护，bypass 下确认。"""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    # 把 repo/.git 变成「文件 + 目录」混合不可能；这里用纯 gitfile 场景另造一个 repo。
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    wtsrc = wt / "src"
    wtsrc.mkdir()
    monkeypatch.chdir(wtsrc)
    fp = str(wt / ".git" / "hooks" / "x")
    assert permissions._is_protected_path(fp) is True
    r = check_permission("write_file", {"file_path": fp}, "bypassPermissions")
    assert r["action"] == "confirm"
    assert "protected" in r["message"]
