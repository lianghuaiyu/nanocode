"""End-to-end smoke test for the vendored SimpleMem running on real lancedb.

This test exercises the full add_dialogue -> finalize -> ask pipeline against a
real on-disk LanceDB store, but with the heavy ML dependencies replaced by
deterministic host-injected callables:

  * embed_callable: returns fixed low-dimensional fake vectors (no torch / ST)
  * llm_callable:   returns canned, JSON-parseable strings

The goal is to prove the pipeline runs end-to-end on lancedb AND that the
lightweight injection path keeps torch / sentence_transformers OUT of the
process. It is NOT a test of retrieval / answer quality.
"""
import sys

import pytest

pytest.importorskip("lancedb")  # SimpleMem backend needs the optional [simplemem] extra

from nanocode._vendor import simplemem


EMBED_DIM = 8


def stub_embed(texts):
    """Deterministic fake embeddings.

    Returns one fixed-dimension vector per input text. The value is derived
    deterministically from the text so identical inputs map to identical
    vectors (which is all lancedb needs for a valid table + search).
    """
    return [[float(len(t) % 7)] * EMBED_DIM for t in texts]


def stub_llm(messages):
    """Deterministic fake LLM.

    SimpleMem calls the LLM from two distinct kinds of call site with two
    different parsing expectations, both routed through extract_json():

      1. MemoryBuilder extraction -> expects a JSON *array* of memory-entry
         objects (each needs at least "lossless_restatement").
      2. HybridRetriever planning / reflection / answer generation -> expect a
         JSON *object*, always read via dict.get(..., default), and
         AnswerGenerator reads the "answer" key.

    We disambiguate by sniffing the user prompt: the extraction prompt is the
    only one that explicitly asks to "Return ONLY the JSON array".
    """
    user_content = ""
    for m in messages:
        if m.get("role") == "user":
            user_content = m.get("content", "")
            break

    if "JSON array" in user_content:
        # MemoryBuilder extraction path -> JSON array of entries.
        return (
            '[{"lossless_restatement": "Alice proposed meeting at 2pm on '
            '2026-06-07.", "keywords": ["Alice", "meet", "2pm"], '
            '"timestamp": "2026-06-07T14:00:00", "location": null, '
            '"persons": ["Alice"], "entities": [], "topic": "meeting"}]'
        )

    # Everything else (planning, reflection, query generation, answer
    # synthesis) reads a JSON object via .get(); provide a superset of keys so
    # no call site crashes, including the "answer" key AnswerGenerator wants.
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


def test_simplemem_vendor_lancedb_smoke(tmp_path):
    db_path = str(tmp_path / "lance")

    mem = simplemem.create(
        mode="text",
        db_path=db_path,
        llm_callable=stub_llm,
        embed_callable=stub_embed,
        embed_dimension=EMBED_DIM,
        clear_db=True,
    )

    # Full pipeline: write -> finalize -> read.
    mem.add_dialogue("Alice", "meet at 2pm tomorrow", "2026-06-06T14:00:00")
    mem.finalize()

    ans = mem.ask("when?")

    # The pipeline must complete and return a (non-None) string answer.
    assert isinstance(ans, str)
    assert ans != ""

    # Sanity: the memory was actually persisted to the real lancedb table.
    memories = mem.get_all_memories()
    assert len(memories) >= 1


def test_no_heavy_ml_imports():
    """Prove the lightweight injection path kept torch / ST out of the process.

    Importing simplemem and running the pipeline above must NOT pull in the
    heavyweight embedding stack. This is the core "轻量化成功" assertion.
    """
    assert "torch" not in sys.modules
    assert "sentence_transformers" not in sys.modules
    assert "transformers" not in sys.modules
