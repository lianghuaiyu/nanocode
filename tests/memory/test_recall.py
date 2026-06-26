import time
from nanocode.memory import recall


def test_memory_age_today():
    assert recall.memory_age(time.time() * 1000) == "today"


def test_freshness_warning_recent_is_empty():
    assert recall.memory_freshness_warning(time.time() * 1000) == ""


def test_freshness_warning_old_warns():
    old = (time.time() - 10 * 86400) * 1000
    assert "10 days old" in recall.memory_freshness_warning(old)


def test_format_injection():
    m = recall.RelevantMemory(path="/x", content="ctext", mtime_ms=0, header="HEADER")
    out = recall.format_memories_for_injection([m])
    assert "<system-reminder>" in out and "ctext" in out and "HEADER" in out
