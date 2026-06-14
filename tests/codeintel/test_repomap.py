"""docs/15 §9：RepoIndex + RepoMapProvider（symbols 抽取 / aider 排名 / 二分预算渲染）。"""

import asyncio
import subprocess

from nanocode.codeintel import RepoIndex, RepoQuery, extract_symbols
from nanocode.context.providers import RepoMapProvider, ContextRequest


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _git_repo(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)


def test_extract_symbols_python(tmp_path):
    p = _write(tmp_path, "m.py", "import os\n\ndef foo():\n    pass\n\nclass Bar:\n    def baz(self):\n        pass\n")
    tags = extract_symbols("m.py", str(p), p.read_text())
    names = {(t.name, t.kind) for t in tags}
    assert ("foo", "def") in names and ("Bar", "def") in names and ("baz", "def") in names
    assert all(t.language == "python" for t in tags)
    foo = next(t for t in tags if t.name == "foo")
    assert foo.line == 3


def test_extract_symbols_js_ts_lexical_fallback(tmp_path, monkeypatch):
    # 显式钉词法回退路径（extra 未装时的行为）；tree-sitter 路径见 test_ts.py。
    import nanocode.codeintel.ts as ts_mod
    monkeypatch.setattr(ts_mod, "ts_available", lambda: False)
    p = _write(tmp_path, "m.ts", "export function run() {}\nclass Svc {}\nexport const make = () => {}\ninterface I {}\ntype T = string\n")
    names = {t.name for t in extract_symbols("m.ts", str(p), p.read_text())}
    assert {"run", "Svc", "make", "I", "T"} <= names


def test_unknown_language_yields_no_tags(tmp_path):
    p = _write(tmp_path, "data.json", '{"a": 1}\n')
    assert extract_symbols("data.json", str(p), p.read_text()) == []


def _files_of(tags):
    return {(t[0] if isinstance(t, tuple) else t.rel_path) for t in tags}


def test_repo_index_scan_and_ranked_tags(tmp_path):
    _write(tmp_path, "a.py", "def alpha_handler():\n    pass\n")
    _write(tmp_path, "b.py", "def beta_worker():\n    pass\ndef gamma():\n    pass\n")
    _write(tmp_path, ".git/x.py", "def hidden():\n    pass\n")     # 跳过 .git
    idx = RepoIndex(str(tmp_path))
    idx.scan_repo()
    rels = _files_of(idx.ranked_tags(RepoQuery()))
    assert "a.py" in rels and "b.py" in rels
    assert not any("x.py" in r for r in rels)                      # .git 被跳过
    # 提及 identifier → 该 def 的文件排第一（×10 ident 加权 + rank 分发）
    tags = idx.ranked_tags(RepoQuery(mentioned_identifiers=["alpha_handler"]))
    first = next(t for t in tags if not isinstance(t, tuple))
    assert first.rel_path == "a.py"


def test_render_map_binary_search_fits_budget(tmp_path):
    for i in range(40):
        _write(tmp_path, f"f{i}.py", "\n".join(f"def fn{i}_{j}():\n    pass" for j in range(10)))
    idx = RepoIndex(str(tmp_path))
    idx.scan_repo()
    from nanocode.context.packs import estimate_tokens
    out = idx.render_map(idx.ranked_tags(RepoQuery()), budget_tokens=200)
    assert "# Repo map" in out
    # aider 二分语义：拟合到预算附近（≤budget 或 15% 容差内），绝不远超
    assert estimate_tokens(out) <= 200 * 1.15
    full = idx.render_map(idx.ranked_tags(RepoQuery()), budget_tokens=100000)
    assert estimate_tokens(full) > 200                             # 预算确实在起约束作用


def test_mtime_cache_skips_unchanged(tmp_path):
    p = _write(tmp_path, "a.py", "def alpha():\n    pass\n")
    idx = RepoIndex(str(tmp_path))
    idx.update([p])
    first = idx.tags("a.py")
    idx.update([p])                                                # mtime 未变 → 不重扫
    assert idx.tags("a.py") is first                              # 同一对象(未替换)


def test_repomap_provider_emits_pack(tmp_path):
    _write(tmp_path, "svc.py", "def serve():\n    pass\nclass Server:\n    pass\n")
    _git_repo(tmp_path)
    pack = asyncio.run(RepoMapProvider().collect(ContextRequest(cwd=str(tmp_path), include_repo_map=True)))
    assert pack is not None and pack.kind == "repo_map"
    assert "svc.py" in pack.as_text() and "serve" in pack.as_text()
    assert pack.persist_policy == "none" and pack.lifecycle == "turn"


def test_repomap_provider_none_when_repo_empty(tmp_path):
    pack = asyncio.run(RepoMapProvider().collect(ContextRequest(cwd=str(tmp_path), include_repo_map=True)))
    assert pack is None                                            # 空仓库（无任何文件）→ 无 pack


def test_repomap_provider_disabled_by_zero_tokens_does_not_scan(tmp_path, monkeypatch):
    _write(tmp_path, "svc.py", "def serve():\n    pass\n")
    _git_repo(tmp_path)
    import nanocode.codeintel as codeintel
    def fail_get_service(_root):
        raise AssertionError("repo map service should not start when map tokens are 0")
    monkeypatch.setattr(codeintel, "get_service", fail_get_service)
    # 预算 0 是 Aider --map-tokens 0 语义：直接禁用，不应触发索引服务。
    pack = asyncio.run(RepoMapProvider().collect(ContextRequest(
        cwd=str(tmp_path), include_repo_map=True, map_tokens=0)))
    assert pack is None


def test_repomap_provider_lists_files_for_docs_only_repo(tmp_path):
    _write(tmp_path, "readme.md", "# hi\n")                        # 无源码符号 → aider 语义:裸文件清单
    _git_repo(tmp_path)
    pack = asyncio.run(RepoMapProvider().collect(ContextRequest(cwd=str(tmp_path), include_repo_map=True)))
    assert pack is not None and "readme.md" in pack.as_text()


def test_no_files_budget_multiplier_capped_by_context_window(tmp_path, monkeypatch):
    _write(tmp_path, "svc.py", "def serve():\n    pass\n")
    _git_repo(tmp_path)
    from nanocode.codeintel import reset_services
    reset_services()
    seen = {}
    from nanocode.codeintel.service import CodeIntelService
    orig = CodeIntelService.repo_map
    def spying(self, query=None, *, budget_tokens=1024, refresh=None, force_refresh=False):
        seen["budget"] = budget_tokens
        return orig(self, query, budget_tokens=budget_tokens, refresh=refresh,
                    force_refresh=force_refresh)
    monkeypatch.setattr(CodeIntelService, "repo_map", spying)
    # 无 personal 文件 + 知道窗口 → 默认 ×2（aider CLI map-multiplier-no-files）
    asyncio.run(RepoMapProvider().collect(ContextRequest(
        cwd=str(tmp_path), include_repo_map=True,
        map_tokens=1000, context_window_tokens=200_000)))
    assert seen["budget"] == 2000
    # 可配置倍率仍保留 Aider RepoMap 类的放大语义。
    asyncio.run(RepoMapProvider().collect(ContextRequest(
        cwd=str(tmp_path), include_repo_map=True,
        map_tokens=1000, context_window_tokens=200_000,
        map_multiplier_no_files=8)))
    assert seen["budget"] == 8000
    # 窗口太小 → 封顶到 window − 4096（aider padding）
    asyncio.run(RepoMapProvider().collect(ContextRequest(
        cwd=str(tmp_path), include_repo_map=True,
        map_tokens=1000, context_window_tokens=6000)))
    assert seen["budget"] == 6000 - 4096
    # 有 personal 文件 → 不放大；mentions 不影响放大（aider 条件只看 chat files）
    asyncio.run(RepoMapProvider().collect(ContextRequest(
        cwd=str(tmp_path), include_repo_map=True, files_read=[str(tmp_path / "svc.py")],
        map_tokens=1000, context_window_tokens=200_000)))
    assert seen["budget"] == 1000
    asyncio.run(RepoMapProvider().collect(ContextRequest(
        cwd=str(tmp_path), include_repo_map=True, user_prompt="look at serve",
        map_tokens=1000, context_window_tokens=200_000)))
    assert seen["budget"] == 2000
    reset_services()
