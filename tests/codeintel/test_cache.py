"""第三档：tags 磁盘缓存（diskcache）+ map 结果缓存 + refresh 四档（aider 复刻）。"""

import pytest

from nanocode.codeintel import RepoQuery, get_service, reset_services
from nanocode.codeintel.cache import TagsCache


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


import importlib.util

_HAS_DISKCACHE = importlib.util.find_spec("diskcache") is not None


# ─── A: tags 磁盘缓存 ─────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_DISKCACHE, reason="diskcache extra not installed")
def test_tags_cache_persists_across_index_instances(tmp_path, monkeypatch):
    _write(tmp_path, "m.py", "def alpha():\n    pass\n")
    from nanocode.codeintel.index import RepoIndex
    idx1 = RepoIndex(str(tmp_path))
    idx1.scan_repo()
    assert idx1.tags_cache.persistent                          # extra 装了 → 落盘
    assert (tmp_path / ".nanocode").exists()
    # 新进程模拟：新 RepoIndex 实例,extract_symbols 不应被调用（磁盘命中）
    import nanocode.codeintel.index as idx_mod
    calls = {"n": 0}
    orig = idx_mod.extract_symbols
    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr(idx_mod, "extract_symbols", counting)
    idx2 = RepoIndex(str(tmp_path))
    idx2.scan_repo()
    assert calls["n"] == 0                                      # 全部命中磁盘,零重解析
    assert {t.name for t in idx2.tags("m.py")} == {"alpha"}


@pytest.mark.skipif(not _HAS_DISKCACHE, reason="diskcache extra not installed")
def test_tags_cache_invalidated_on_mtime_change(tmp_path):
    p = _write(tmp_path, "m.py", "def alpha():\n    pass\n")
    from nanocode.codeintel.index import RepoIndex
    RepoIndex(str(tmp_path)).scan_repo()                       # 填缓存
    import os, time
    time.sleep(0.01)
    p.write_text("def beta():\n    pass\n")
    os.utime(p, None)
    idx = RepoIndex(str(tmp_path))
    idx.scan_repo()
    assert {t.name for t in idx.tags("m.py")} == {"beta"}      # mtime 变 → 重抽取


def test_tags_cache_memory_fallback_works(tmp_path, monkeypatch):
    # diskcache 不可用 → 内存 dict（进程内仍正确,跨进程不持久）。
    import nanocode.codeintel.cache as cache_mod
    monkeypatch.setattr(cache_mod, "_open_diskcache", lambda path: None)
    tc = TagsCache(str(tmp_path))
    assert not tc.persistent
    tc.set("/x/a.py", 1.0, ["sentinel"])
    assert tc.get("/x/a.py", 1.0) == ["sentinel"]
    assert tc.get("/x/a.py", 2.0) is None                      # mtime 不符 → miss


@pytest.mark.skipif(not _HAS_DISKCACHE, reason="diskcache extra not installed")
def test_tags_cache_recovers_from_corruption(tmp_path):
    tc = TagsCache(str(tmp_path))
    assert tc.persistent
    import sqlite3
    tc._recover(sqlite3.DatabaseError("boom"))                 # 触发删目录重建
    tc.set("/x/a.py", 1.0, ["ok"])
    assert tc.get("/x/a.py", 1.0) == ["ok"]                    # 重建后可用


# ─── B: map 结果缓存 + refresh 四档 ───────────────────────────────────────────

def _spy_uncached(svc, monkeypatch):
    calls = {"n": 0}
    orig = svc._repo_map_uncached
    def counting(q, budget):
        calls["n"] += 1
        return orig(q, budget)
    monkeypatch.setattr(svc, "_repo_map_uncached", counting)
    return calls


def test_refresh_files_uses_cache(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "def fn():\n    pass\n")
    svc = get_service(str(tmp_path))
    calls = _spy_uncached(svc, monkeypatch)
    q = RepoQuery()
    svc.repo_map(q, budget_tokens=500, refresh="files")
    svc.repo_map(q, budget_tokens=500, refresh="files")
    assert calls["n"] == 1                                      # 第二次命中缓存
    svc.repo_map(q, budget_tokens=999, refresh="files")        # budget 变 → 新 key
    assert calls["n"] == 2


def test_refresh_always_never_caches(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "def fn():\n    pass\n")
    svc = get_service(str(tmp_path))
    calls = _spy_uncached(svc, monkeypatch)
    for _ in range(3):
        svc.repo_map(RepoQuery(), budget_tokens=500, refresh="always")
    assert calls["n"] == 3


def test_refresh_manual_pins_first_map(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "def fn():\n    pass\n")
    svc = get_service(str(tmp_path))
    calls = _spy_uncached(svc, monkeypatch)
    svc.repo_map(RepoQuery(), budget_tokens=500, refresh="manual")
    svc.repo_map(RepoQuery(mentioned_identifiers=["fn"]), budget_tokens=99, refresh="manual")
    assert calls["n"] == 1                                      # manual：首算后任何 query 都返回 last_map
    out = svc.repo_map(RepoQuery(), budget_tokens=500, refresh="manual", force_refresh=True)
    assert calls["n"] == 2 and out is not None                 # force 绕过


def test_refresh_auto_caches_only_when_slow(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "def fn():\n    pass\n")
    svc = get_service(str(tmp_path))
    calls = _spy_uncached(svc, monkeypatch)
    q = RepoQuery()
    svc.repo_map(q, budget_tokens=500, refresh="auto")         # 快（<1s）→ 不读缓存
    assert svc._map_processing_time < 1.0
    svc.repo_map(q, budget_tokens=500, refresh="auto")
    assert calls["n"] == 2                                      # 仍重算（auto 只在贵时吃缓存）
    svc._map_processing_time = 2.0                             # 模拟上次构建很慢
    svc.repo_map(q, budget_tokens=500, refresh="auto")
    svc.repo_map(q, budget_tokens=500, refresh="auto")
    assert calls["n"] == 2                                      # 命中已写入的缓存（不再增）


def test_invalid_refresh_falls_back_to_auto(tmp_path):
    from nanocode.codeintel.service import CodeIntelService
    svc = CodeIntelService(str(tmp_path), refresh="bogus")
    assert svc.refresh == "auto"


def test_context_config_exposes_refresh(tmp_path, monkeypatch):
    from nanocode.tools import reset_permission_cache
    from nanocode.tools.permissions import load_context_config
    (tmp_path / ".nanocode").mkdir()
    (tmp_path / ".nanocode" / "settings.json").write_text(
        '{"context": {"repo_map_refresh": "files"}}')
    monkeypatch.chdir(tmp_path)
    reset_permission_cache()
    assert load_context_config()["repo_map_refresh"] == "files"
    reset_permission_cache()
    # 非法值 → auto
    (tmp_path / ".nanocode" / "settings.json").write_text(
        '{"context": {"repo_map_refresh": "nope"}}')
    reset_permission_cache()
    assert load_context_config()["repo_map_refresh"] == "auto"
    reset_permission_cache()
