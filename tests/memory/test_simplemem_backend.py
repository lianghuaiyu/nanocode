import sys
import pytest

pytest.importorskip("lancedb")  # SimpleMem backend needs the optional [simplemem] extra

from nanocode._vendor import simplemem
from nanocode.memory.backend import SimpleMemBackend
from nanocode.memory.recall import RelevantMemory

EMBED_DIM = 8


def stub_embed(texts):
    return [[float(len(t) % 7)] * EMBED_DIM for t in texts]


def stub_llm(messages):
    user = ""
    for m in messages:
        if m.get("role") == "user":
            user = m.get("content", "")
            break
    if "JSON array" in user:
        return (
            '[{"lossless_restatement": "Alice proposed meeting at 2pm on '
            '2026-06-07.", "keywords": ["Alice", "meet", "2pm"], '
            '"timestamp": "2026-06-07T14:00:00", "location": null, '
            '"persons": ["Alice"], "entities": [], "topic": "meeting"}]'
        )
    return (
        '{"answer": "ok", "keywords": ["meet"], "persons": [], '
        '"entities": [], "location": null, "time_expression": null, '
        '"queries": ["when?"], "assessment": "sufficient", '
        '"coverage_percentage": 100, "additional_queries": [], '
        '"targeted_queries": [], "question_type": "general", '
        '"key_entities": [], "required_info": [], "relationships": [], '
        '"minimal_queries_needed": 1, "missing_info": [], '
        '"missing_info_types": [], "reasoning": "stub"}'
    )


def _make_system(tmp_path):
    return simplemem.create(
        mode="text",
        db_path=str(tmp_path / "lance"),
        llm_callable=stub_llm,
        embed_callable=stub_embed,
        embed_dimension=EMBED_DIM,
        clear_db=True,
        enable_planning=False,      # decision 4：热路径关 planning/reflection
        enable_reflection=False,
    )


def test_simplemem_backend_name(tmp_path):
    b = SimpleMemBackend(system=_make_system(tmp_path), db_path=str(tmp_path / "lance"))
    assert b.name == "simplemem"


def test_simplemem_record_then_retrieve(tmp_path):
    b = SimpleMemBackend(system=_make_system(tmp_path), db_path=str(tmp_path / "lance"))
    b.record_observation("Alice", "meet at 2pm tomorrow", "2026-06-06T14:00:00")
    hits = b.retrieve("when do we meet", limit=5)
    assert isinstance(hits, list)
    assert all(isinstance(h, RelevantMemory) for h in hits)
    assert len(hits) >= 1
    assert any("meet" in h.content.lower() or "alice" in h.content.lower() for h in hits)


def test_simplemem_retrieve_returns_relevant_memory_shape(tmp_path):
    b = SimpleMemBackend(system=_make_system(tmp_path), db_path=str(tmp_path / "lance"))
    b.record_observation("Alice", "meet at 2pm tomorrow", "2026-06-06T14:00:00")
    hits = b.retrieve("meet", limit=5)
    h = hits[0]
    assert h.path.startswith("simplemem://")
    assert isinstance(h.content, str) and h.content
    assert "SimpleMem" in h.header


def test_simplemem_retrieve_silences_stdout(tmp_path, capsys):
    b = SimpleMemBackend(system=_make_system(tmp_path), db_path=str(tmp_path / "lance"))
    b.record_observation("Alice", "meet at 2pm tomorrow", "2026-06-06T14:00:00")
    capsys.readouterr()  # 清掉之前
    b.retrieve("meet", limit=5)
    captured = capsys.readouterr()
    assert "=" * 10 not in captured.out  # SimpleMem 的 print 分隔线被静音


def test_simplemem_stats(tmp_path):
    b = SimpleMemBackend(system=_make_system(tmp_path), db_path=str(tmp_path / "lance"))
    b.record_observation("Alice", "meet at 2pm tomorrow", "2026-06-06T14:00:00")
    s = b.stats()
    assert s["backend"] == "simplemem"
    assert s["count"] >= 1


def test_simplemem_no_heavy_ml_imports(tmp_path):
    b = SimpleMemBackend(system=_make_system(tmp_path), db_path=str(tmp_path / "lance"))
    b.record_observation("Alice", "x", "2026-06-06T14:00:00")
    b.retrieve("x", limit=3)
    assert "torch" not in sys.modules
    assert "sentence_transformers" not in sys.modules
