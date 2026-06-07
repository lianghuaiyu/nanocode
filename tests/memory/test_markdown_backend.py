from nanocode.memory.backend import MarkdownMemoryBackend
from nanocode.memory import store


def test_markdown_backend_name():
    assert MarkdownMemoryBackend().name == "markdown"


def test_markdown_retrieve_keyword_hit():
    store.save_memory("Pin layout", "about pins", "project", "the pin body mentions pins")
    hits = MarkdownMemoryBackend().retrieve("pins", limit=5)
    assert len(hits) >= 1
    assert hits[0].path.endswith("project_pin_layout.md")
    assert "pin" in hits[0].content.lower()
    assert hits[0].header  # 有新鲜度/路径头


def test_markdown_retrieve_no_match_empty():
    store.save_memory("Pin", "about pins", "project", "pins")
    assert MarkdownMemoryBackend().retrieve("zzzznomatch", limit=5) == []


def test_markdown_retrieve_empty_dir():
    assert MarkdownMemoryBackend().retrieve("anything", limit=5) == []


def test_markdown_stats_counts_files():
    store.save_memory("A", "a", "project", "x")
    store.save_memory("B", "b", "project", "y")
    s = MarkdownMemoryBackend().stats()
    assert s["backend"] == "markdown"
    assert s["count"] >= 2


def test_markdown_record_observation_noop():
    MarkdownMemoryBackend().record_observation("user", "hi")  # no raise
