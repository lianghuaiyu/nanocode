"""docs/22 Phase 1: RetrievalConfig schema + config-driven no-LLM retrieval."""
import pytest

pytest.importorskip("lancedb")

from nanocode.memory.engines.simplemem import (
    SimpleMemConfig, MemoryNote, RetrievalConfig, create_simplemem_engine,
)
from nanocode.memory.engines.simplemem.retrieval_config import FUSION_MODES

EMBED_DIM = 16


def make_embedder():
    def embed(texts):
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            for tok in t.lower().split():
                v[sum(ord(c) for c in tok) % EMBED_DIM] += 1.0
            out.append(v)
        return out
    return embed


# ── RetrievalConfig schema ──────────────────────────────────────────

def test_defaults_match_legacy_behaviour():
    c = RetrievalConfig()
    assert c.semantic_top_k == 25 and c.keyword_top_k == 5 and c.structured_top_k == 5
    assert c.max_context == 5 and c.fusion_mode == "rrf"
    assert c.weight_timestamp == 0.6 and c.lexical_exact_boost == 0.0
    assert c.time_decay_half_life_days is None


def test_from_dict_rejects_unknown_field_fail_loud():
    with pytest.raises(ValueError):
        RetrievalConfig.from_dict({"semantic_top_k": 10, "bogus": 1})


def test_validate_rejects_bad_fusion_mode():
    with pytest.raises(ValueError):
        RetrievalConfig(fusion_mode="magic")


def test_validate_rejects_negative_and_zero_context():
    with pytest.raises(ValueError):
        RetrievalConfig(semantic_top_k=-1)
    with pytest.raises(ValueError):
        RetrievalConfig(max_context=0)
    with pytest.raises(ValueError):
        RetrievalConfig(weight_semantic=-0.1)
    with pytest.raises(ValueError):
        RetrievalConfig(time_decay_half_life_days=0)


def test_to_dict_roundtrip_stable():
    c = RetrievalConfig(semantic_top_k=10, fusion_mode="keyword_only")
    d = c.to_dict()
    assert d["fusion_mode"] == "keyword_only"
    assert RetrievalConfig.from_dict(d) == c
    assert list(d)[0] == "schema_version"  # stable order


def test_all_fusion_modes_valid():
    for m in FUSION_MODES:
        RetrievalConfig(fusion_mode=m).validate()


# ── config-driven retrieval ─────────────────────────────────────────

def _engine(tmp_path, retrieval=None):
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM,
                          retrieval=retrieval or RetrievalConfig())
    return create_simplemem_engine(cfg, llm=None, embed=make_embedder(), data_root=str(tmp_path))


def test_validate_rejects_bool_and_out_of_range():
    # bool is an int subclass — must be rejected, not coerced to 0/1 (review fix).
    with pytest.raises(ValueError):
        RetrievalConfig(semantic_top_k=True)
    with pytest.raises(ValueError):
        RetrievalConfig(max_context=False)
    with pytest.raises(ValueError):
        RetrievalConfig(weight_semantic=True)
    # finite upper bounds keep absurd corrupt-config values off the hot path.
    with pytest.raises(ValueError):
        RetrievalConfig(semantic_top_k=10_000_000)
    with pytest.raises(ValueError):
        RetrievalConfig(max_context=10_000_000)


def test_validate_rejects_nan_and_inf():
    # NaN/inf comparisons are all False; must be rejected explicitly (review fix).
    with pytest.raises(ValueError):
        RetrievalConfig(weight_semantic=float("nan"))
    with pytest.raises(ValueError):
        RetrievalConfig(weight_keyword=float("inf"))
    with pytest.raises(ValueError):
        RetrievalConfig(time_decay_half_life_days=float("nan"))


def test_parse_epoch_days_is_timezone_safe():
    # Z, explicit +00:00, and naive (anchored to UTC) must all agree, regardless
    # of host local timezone (review fix: no host-TZ-dependent ranking).
    from nanocode.memory.engines.simplemem.retriever import _parse_epoch_days
    z = _parse_epoch_days("2024-01-01T00:00:00Z")
    off = _parse_epoch_days("2024-01-01T00:00:00+00:00")
    naive = _parse_epoch_days("2024-01-01T00:00:00")
    assert z == off == naive
    assert _parse_epoch_days(None) is None
    assert _parse_epoch_days("garbage") is None


def test_max_context_caps_results(tmp_path):
    eng = _engine(tmp_path, RetrievalConfig(max_context=2))
    for i in range(6):
        eng.add_note(MemoryNote(title=f"deploy {i}", content=f"deploy the fleet step {i}"))
    hits = eng.retrieve_fast("deploy fleet", limit=10)
    assert len(hits) <= 2  # capped by max_context, not the larger limit


def test_retrieve_with_config_does_not_mutate_live(tmp_path):
    eng = _engine(tmp_path, RetrievalConfig(max_context=5))
    for i in range(6):
        eng.add_note(MemoryNote(title=f"deploy {i}", content=f"deploy fleet {i}"))
    capped = eng.retrieve_with_config("deploy fleet", RetrievalConfig(max_context=1), limit=10)
    assert len(capped) == 1
    # live retriever is unchanged
    assert len(eng.retrieve_fast("deploy fleet", limit=10)) <= 5
    assert len(eng.retrieve_fast("deploy fleet", limit=10)) > 1


def test_keyword_only_mode_no_llm(tmp_path):
    eng = _engine(tmp_path, RetrievalConfig(fusion_mode="keyword_only"))
    eng.add_note(MemoryNote(title="kubernetes", content="kubernetes cluster autoscaling"))
    hits = eng.retrieve_fast("kubernetes", limit=5)
    assert any("kubernetes" in h.lossless_restatement.lower() for h in hits)
