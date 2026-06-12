"""docs/15 Phase 4 §9：词法 RepoIndex + RepoMapProvider（defs 抽取 / 个性化排名 / 预算渲染）。"""

import asyncio

from nanocode.codeintel import RepoIndex, RepoQuery, extract_symbols
from nanocode.context.providers import RepoMapProvider, ContextRequest


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_extract_symbols_python(tmp_path):
    p = _write(tmp_path, "m.py", "import os\n\ndef foo():\n    pass\n\nclass Bar:\n    def baz(self):\n        pass\n")
    tags = extract_symbols("m.py", str(p), p.read_text())
    names = {(t.name, t.kind) for t in tags}
    assert ("foo", "def") in names and ("Bar", "def") in names and ("baz", "def") in names
    assert all(t.language == "python" for t in tags)
    foo = next(t for t in tags if t.name == "foo")
    assert foo.line == 3


def test_extract_symbols_js_ts(tmp_path):
    p = _write(tmp_path, "m.ts", "export function run() {}\nclass Svc {}\nexport const make = () => {}\ninterface I {}\ntype T = string\n")
    names = {t.name for t in extract_symbols("m.ts", str(p), p.read_text())}
    assert {"run", "Svc", "make", "I", "T"} <= names


def test_unknown_language_yields_no_tags(tmp_path):
    p = _write(tmp_path, "data.json", '{"a": 1}\n')
    assert extract_symbols("data.json", str(p), p.read_text()) == []


def test_repo_index_scan_and_rank_personalization(tmp_path):
    _write(tmp_path, "a.py", "def alpha():\n    pass\n")
    _write(tmp_path, "b.py", "def beta():\n    pass\ndef gamma():\n    pass\n")
    _write(tmp_path, ".git/x.py", "def hidden():\n    pass\n")     # 跳过 .git
    idx = RepoIndex(str(tmp_path))
    idx.scan_repo()
    rels = {rf.rel_path for rf in idx.rank(RepoQuery())}
    assert "a.py" in rels and "b.py" in rels
    assert not any("x.py" in r for r in rels)                      # .git 被跳过
    # 提及 identifier alpha → a.py 排名高于 b.py
    ranked = idx.rank(RepoQuery(mentioned_identifiers=["alpha"]))
    assert ranked[0].rel_path == "a.py"


def test_repo_index_render_budget_truncates(tmp_path):
    for i in range(40):
        _write(tmp_path, f"f{i}.py", "\n".join(f"def fn{i}_{j}():\n    pass" for j in range(10)))
    idx = RepoIndex(str(tmp_path))
    idx.scan_repo()
    out = idx.render(idx.rank(RepoQuery()), budget_tokens=200)
    assert "# Repo map (lexical)" in out
    assert "truncated" in out                                      # 预算封顶截断


def test_mtime_cache_skips_unchanged(tmp_path):
    p = _write(tmp_path, "a.py", "def alpha():\n    pass\n")
    idx = RepoIndex(str(tmp_path))
    idx.update([p])
    first = idx.tags("a.py")
    idx.update([p])                                                # mtime 未变 → 不重扫
    assert idx.tags("a.py") is first                              # 同一对象(未替换)


def test_repomap_provider_emits_pack(tmp_path):
    _write(tmp_path, "svc.py", "def serve():\n    pass\nclass Server:\n    pass\n")
    pack = asyncio.run(RepoMapProvider().collect(ContextRequest(cwd=str(tmp_path), include_repo_map=True)))
    assert pack is not None and pack.kind == "repo_map"
    assert "svc.py" in pack.as_text() and "serve" in pack.as_text()
    assert pack.persist_policy == "none" and pack.lifecycle == "turn"


def test_repomap_provider_none_when_no_symbols(tmp_path):
    _write(tmp_path, "readme.md", "# hi\n")                        # 无源码符号
    pack = asyncio.run(RepoMapProvider().collect(ContextRequest(cwd=str(tmp_path), include_repo_map=True)))
    assert pack is None
