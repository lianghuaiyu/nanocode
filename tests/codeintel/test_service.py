"""codeintel/service：嵌入面门面——零 agent 耦合、进程级缓存、点查、提及提取。"""

import pytest

from nanocode.codeintel import RepoQuery, get_service, reset_services


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


def test_service_is_agent_free():
    # 嵌入面硬约束：codeintel 不得 import agent 层。
    import sys
    for m in list(sys.modules):
        if m.startswith("nanocode.codeintel"):
            del sys.modules[m]
    import nanocode.codeintel  # noqa: F401
    src = open(nanocode.codeintel.service.__file__).read()
    assert "from ..agent" not in src and "import nanocode.agent" not in src


def test_repo_map_personalized_and_budgeted(tmp_path):
    _write(tmp_path, "app.py", "from lib import helper\ndef run():\n    helper()\n")
    _write(tmp_path, "lib.py", "def helper():\n    pass\n")
    _write(tmp_path, "noise.py", "def boring():\n    pass\n")
    svc = get_service(str(tmp_path))
    r = svc.repo_map(RepoQuery(files_read=[str(tmp_path / "app.py")]), budget_tokens=500)
    assert "lib.py" in r.text and "helper" in r.text
    assert "app.py:" not in r.text                     # personal 不渲染
    assert r.token_estimate <= 500 * 1.15


def test_index_cached_across_calls(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "def alpha():\n    pass\n")
    svc = get_service(str(tmp_path))
    svc.repo_map(RepoQuery())
    calls = {"n": 0}

    import nanocode.codeintel.index as idx_mod
    orig_scan = idx_mod.RepoIndex.scan_repo
    def counting_scan(self, **kw):
        calls["n"] += 1
        return orig_scan(self, **kw)
    monkeypatch.setattr(idx_mod.RepoIndex, "scan_repo", counting_scan)
    svc.repo_map(RepoQuery())
    svc.defs("a.py")
    assert calls["n"] == 0                             # 已扫过 → 不再全扫（跨调用复用索引）
    assert get_service(str(tmp_path)) is svc           # per-root 单例


def test_find_definition_and_def_names(tmp_path):
    _write(tmp_path, "m.py", "class Box:\n    def open(self):\n        pass\n")
    svc = get_service(str(tmp_path))
    hits = svc.find_definition("Box.open")
    assert len(hits) == 1 and hits[0].rel_path == "m.py"
    assert {"Box", "open", "Box.open"} <= svc.def_names()


def test_extract_mentions_matches_idents_and_files(tmp_path):
    _write(tmp_path, "session.py", "def acquire_lease():\n    pass\n")
    _write(tmp_path, "other.py", "def misc():\n    pass\n")
    svc = get_service(str(tmp_path))
    idents, files = svc.extract_mentions("please fix acquire_lease in session.py")
    assert idents == ["acquire_lease"]
    assert "session.py" in files
    assert svc.extract_mentions("") == ([], [])
