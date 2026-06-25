from nanocode.memory.markdown_backend import MarkdownMemoryBackend, OffMemoryBackend
from nanocode.memory import store


def test_name():
    assert MarkdownMemoryBackend().name == "markdown"


def test_retrieve_fast_keyword_hit():
    store.save_memory("Pin layout", "about pins", "project", "the pin body mentions pins")
    hits = MarkdownMemoryBackend().retrieve_fast("pins", limit=5, token_budget=0)
    assert hits and hits[0].ref.endswith("project_pin_layout.md")
    assert hits[0].kind == "project" and hits[0].score > 0
    assert "pin" in hits[0].content.lower()


def test_retrieve_fast_no_match_empty():
    store.save_memory("Pin", "about pins", "project", "pins")
    assert MarkdownMemoryBackend().retrieve_fast("zzzznomatch", limit=5, token_budget=0) == []


def test_list_and_read():
    store.save_memory("Alpha", "first", "user", "alpha body")
    b = MarkdownMemoryBackend()
    res = b.list(limit=50)
    assert res.total == 1 and res.entries[0].title == "Alpha"
    ref = res.entries[0].ref
    rd = b.read(ref, max_bytes=8192)
    assert rd.found and "alpha body" in rd.content


def test_read_unknown():
    rd = MarkdownMemoryBackend().read("nope.md", max_bytes=8192)
    assert not rd.found


def test_add_note_then_search():
    b = MarkdownMemoryBackend()
    w = b.add_note(title="Deploy", kind="project", content="deploy via fleet",
                   metadata={"description": "how to deploy"})
    assert w.ok and w.ref
    hits = b.retrieve_fast("deploy", limit=5, token_budget=0)
    assert any("deploy" in h.content.lower() for h in hits)


def test_add_note_coerces_invalid_kind():
    b = MarkdownMemoryBackend()
    w = b.add_note(title="X", kind="note", content="x", metadata={})
    assert w.ok and w.ref.startswith("project_")  # unknown kind -> project


def test_update_and_archive():
    b = MarkdownMemoryBackend()
    fn = b.add_note(title="Tmp", kind="project", content="orig", metadata={}).ref
    u = b.update(fn, content="new body", metadata=None)
    assert u.ok
    assert "new body" in b.read(fn, max_bytes=8192).content
    a = b.archive(fn, reason="cleanup")
    assert a.ok
    assert not b.read(fn, max_bytes=8192).found


def test_stats():
    store.save_memory("A", "a", "project", "x")
    s = MarkdownMemoryBackend().stats()
    assert s["backend"] == "markdown" and s["count"] == 1


def test_off_backend():
    b = OffMemoryBackend()
    assert b.name == "off"
    assert b.retrieve_fast("anything", limit=5, token_budget=0) == []
    assert b.list().entries == []
    assert not b.read("x", max_bytes=10).found
    assert b.add_note(title="t", kind="note", content="c", metadata={}).unsupported
    assert b.update("x", content="c", metadata=None).unsupported
    assert b.archive("x", reason="r").unsupported
    assert b.stats()["backend"] == "off"
