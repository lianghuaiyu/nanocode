"""发现层（git ls-files / rglob 回退 / 截断打标）+ 裸文件尾巴与 special 组装（aider 对照）。"""

import shutil
import subprocess

import pytest

from nanocode.codeintel import RepoIndex, RepoQuery, get_service, reset_services
from nanocode.codeintel.special import filter_important_files, is_important


@pytest.fixture(autouse=True)
def _fresh():
    reset_services()
    yield
    reset_services()


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ─── special.py（aider/special.py 逐字 vendor）────────────────────────────────

def test_is_important_matches_aider_rules():
    assert is_important("README.md") and is_important("pyproject.toml")
    assert is_important(".github/workflows/ci.yml")            # workflows 特判
    assert not is_important("src/README.md")                   # 只认仓库根
    assert not is_important("notes.md")
    assert filter_important_files(["README.md", "a.py"]) == ["README.md"]


# ─── git ls-files 发现 ────────────────────────────────────────────────────────

_git = shutil.which("git")


def _git_repo(tmp_path):
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a],
                                    capture_output=True, check=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True, check=True)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    return run


@pytest.mark.skipif(not _git, reason="git not available")
def test_scan_uses_git_ls_files_and_respects_gitignore(tmp_path):
    run = _git_repo(tmp_path)
    _write(tmp_path, "a.py", "def tracked_fn():\n    pass\n")
    _write(tmp_path, "README.md", "# x\n")
    _write(tmp_path, ".gitignore", "ignored.py\n")
    _write(tmp_path, "ignored.py", "def ignored_fn():\n    pass\n")
    run("add", "a.py", "README.md", ".gitignore")
    run("commit", "-q", "-m", "init")
    _write(tmp_path, "untracked.py", "def untracked_fn():\n    pass\n")
    idx = RepoIndex(str(tmp_path))
    idx.scan_repo()
    assert "a.py" in idx.all_files() and "README.md" in idx.all_files()
    assert "ignored.py" not in idx.all_files()                 # .gitignore 生效
    assert "untracked.py" not in idx.all_files()               # tracked-only（aider 同款）
    assert idx.tags("a.py")


@pytest.mark.skipif(not _git, reason="git not available")
def test_scan_falls_back_to_rglob_when_root_not_toplevel(tmp_path):
    run = _git_repo(tmp_path)
    _write(tmp_path, "x.py", "def top():\n    pass\n")
    run("add", "x.py")
    run("commit", "-q", "-m", "init")
    sub = tmp_path / "sub"
    _write(tmp_path, "sub/inner.py", "def inner_fn():\n    pass\n")   # 未 track
    idx = RepoIndex(str(sub))                                   # root ≠ toplevel → rglob
    idx.scan_repo()
    assert "inner.py" in idx.all_files()


def test_scan_rglob_fallback_and_truncation_flag(tmp_path):
    _write(tmp_path, "a.py", "def fa():\n    pass\n")
    _write(tmp_path, "b.py", "def fb():\n    pass\n")
    _write(tmp_path, "node_modules/x.py", "def vendored():\n    pass\n")
    idx = RepoIndex(str(tmp_path))
    idx.scan_repo(max_files=1)
    assert idx.truncated                                        # 截断不静默
    assert "a.py" in idx.all_files() and "b.py" in idx.all_files()   # 发现清单不截
    assert not any("node_modules" in f for f in idx.all_files())
    idx2 = RepoIndex(str(tmp_path))
    idx2.scan_repo()
    assert not idx2.truncated


# ─── 裸文件尾巴 + special 组装（aider get_ranked_tags 尾部语义）────────────────

def test_bare_file_tail_includes_non_source_files(tmp_path):
    _write(tmp_path, "app.py", "from lib import helper\ndef run():\n    helper()\n")
    _write(tmp_path, "lib.py", "def helper():\n    pass\n")
    _write(tmp_path, "README.md", "# hi\n")
    svc = get_service(str(tmp_path))
    r = svc.repo_map(RepoQuery(), budget_tokens=100_000)
    assert "README.md" in r.text                               # 预算大 → 全量文件可见
    # rank 优先体现在选择（预算紧时图文件存活、尾巴文件被裁）,显示序是字母序（aider 解耦）
    tight = svc.repo_map(RepoQuery(), budget_tokens=10)
    assert "helper" in tight.text or "app.py" in tight.text


def test_personal_files_excluded_from_bare_tail(tmp_path):
    _write(tmp_path, "app.py", "def run():\n    pass\n")
    _write(tmp_path, "lib.py", "def helper():\n    pass\n")
    svc = get_service(str(tmp_path))
    r = svc.repo_map(RepoQuery(files_read=[str(tmp_path / "app.py")]), budget_tokens=100_000)
    assert "app.py" not in r.text                              # personal 连尾巴也不进


def test_special_prepend_is_noop_with_full_tail(tmp_path):
    # aider 源码同款行为：尾巴已含全部文件 → special 前置过滤为空（不重复列 README）。
    _write(tmp_path, "a.py", "def fa():\n    pass\n")
    _write(tmp_path, "README.md", "# hi\n")
    svc = get_service(str(tmp_path))
    r = svc.repo_map(RepoQuery(), budget_tokens=100_000)
    assert r.text.count("README.md") == 1
