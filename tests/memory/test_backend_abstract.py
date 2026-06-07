import pytest
from nanocode.memory.backend import (
    MemoryBackend, OffMemoryBackend, ImportResult,
)


def test_off_backend_name():
    assert OffMemoryBackend().name == "off"


def test_off_backend_retrieve_empty():
    assert OffMemoryBackend().retrieve("anything", limit=5) == []


def test_off_backend_record_is_noop():
    OffMemoryBackend().record_observation("user", "hello")  # must not raise


def test_off_backend_import_zero():
    r = OffMemoryBackend().import_markdown_memories()
    assert isinstance(r, ImportResult)
    assert r.imported == 0 and r.skipped == 0 and r.errors == []


def test_off_backend_stats():
    s = OffMemoryBackend().stats()
    assert s["backend"] == "off"


def test_base_retrieve_not_implemented():
    with pytest.raises(NotImplementedError):
        MemoryBackend().retrieve("q", limit=1)


def test_import_result_defaults():
    r = ImportResult()
    assert (r.imported, r.skipped, r.errors) == (0, 0, [])
