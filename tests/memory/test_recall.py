import asyncio
import time
import pytest
from nanocode.memory import recall, store


def test_prefetch_single_word_returns_none():
    assert recall.start_memory_prefetch("word", lambda s, u: "", set(), 0) is None


def test_memory_age_today():
    assert recall.memory_age(time.time() * 1000) == "today"


def test_freshness_warning_recent_is_empty():
    assert recall.memory_freshness_warning(time.time() * 1000) == ""


def test_format_injection():
    m = recall.RelevantMemory(path="/x", content="ctext", mtime_ms=0, header="HEADER")
    out = recall.format_memories_for_injection([m])
    assert "<system-reminder>" in out and "ctext" in out


def test_select_relevant_memories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store.save_memory("Pin", "about pins", "project", "body about pins")

    async def fake_side_query(system, user):
        return '{"selected_memories": ["project_pin.md"]}'

    res = asyncio.run(recall.select_relevant_memories("tell me about pins", fake_side_query, set()))
    assert len(res) == 1
    assert res[0].path.endswith("project_pin.md")


def test_recall_reads_nested_metadata_type():
    from nanocode.memory import store, recall
    d = store.get_memory_dir()
    (d / "feedback_y.md").write_text(
        '---\nname: y\ndescription: "d"\nmetadata:\n  type: feedback\n---\nbody'
    )
    hdrs = [h for h in recall.scan_memory_headers() if h.filename == "feedback_y.md"]
    assert hdrs and hdrs[0].type == "feedback"
