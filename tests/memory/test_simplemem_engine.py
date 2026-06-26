import json

import pytest

pytest.importorskip("lancedb")  # SimpleMem engine needs the optional [simplemem] extra

from nanocode.memory.engines.simplemem import (
    SimpleMemConfig, SimpleMemEngine, MemoryNote, Dialogue,
    create_simplemem_engine, MigrationRequired, EngineUnavailable, SCHEMA_VERSION,
)
from nanocode.memory.engines.simplemem.migrations import resolve_scoped_root, schema_path
from nanocode.memory.engines.simplemem.errors import SimpleMemError

EMBED_DIM = 16


def make_embedder():
    calls = {"n": 0}

    def embed(texts):
        calls["n"] += 1
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            for tok in t.lower().split():
                v[sum(ord(c) for c in tok) % EMBED_DIM] += 1.0
            out.append(v)
        return out

    return embed, calls


def make_llm():
    calls = {"n": 0}

    def llm(messages):
        calls["n"] += 1
        return ('[{"lossless_restatement": "Alice met Bob on 2026-06-07.", '
                '"keywords": ["Alice", "Bob"], "timestamp": "2026-06-07T00:00:00", '
                '"location": null, "persons": ["Alice", "Bob"], "entities": [], '
                '"topic": "meeting"}]')

    return llm, calls


def _engine(tmp_path, *, with_llm=True):
    embed, ecalls = make_embedder()
    llm, lcalls = (make_llm() if with_llm else (None, {"n": 0}))
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM)
    eng = create_simplemem_engine(cfg, llm=llm, embed=embed, data_root=str(tmp_path))
    return eng, ecalls, lcalls


def test_add_note_then_retrieve_fast_no_llm(tmp_path, capsys):
    eng, ecalls, lcalls = _engine(tmp_path)
    eng.add_note(MemoryNote(title="Deploy guide", content="deploy the service via fleet"))
    hits = eng.retrieve_fast("deploy", limit=5)
    assert hits and any("deploy" in h.lossless_restatement.lower() for h in hits)
    assert lcalls["n"] == 0          # fast retrieval never calls the LLM
    assert capsys.readouterr().out == ""  # engine never prints


def test_retrieve_fast_merges_dedups(tmp_path):
    eng, _, _ = _engine(tmp_path)
    eng.add_note(MemoryNote(title="Fleet deploy", content="deploy via fleet tooling"))
    hits = eng.retrieve_fast("deploy fleet", limit=10)
    ids = [h.entry_id for h in hits]
    assert len(ids) == len(set(ids))  # no duplicate entries across search paths


def test_add_dialogues_uses_llm(tmp_path):
    eng, _, lcalls = _engine(tmp_path)
    produced = eng.add_dialogues([Dialogue(1, "Alice", "Met Bob", "2026-06-07T00:00:00")])
    assert produced and lcalls["n"] >= 1


def test_add_dialogues_without_llm_fails_loud(tmp_path):
    eng, _, _ = _engine(tmp_path, with_llm=False)
    with pytest.raises(EngineUnavailable):
        eng.add_dialogues([Dialogue(1, "A", "hi")])


def test_add_dialogues_does_not_carry_previous_between_jobs(tmp_path):
    # A fresh MemoryBuilder per add_dialogues() job: the 2nd job's prompt must not
    # contain the 1st job's extracted restatement (no cross-job _previous leak,
    # docs/21 §7.3). _previous dedup context only spans dialogues within one job.
    import json as _json
    prompts = []

    def llm(messages):
        prompts.append(messages[-1]["content"])
        return _json.dumps([{"lossless_restatement": f"JOB-FACT-{len(prompts)}",
                             "keywords": [], "timestamp": None, "location": None,
                             "persons": [], "entities": [], "topic": None}])

    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM)
    eng = create_simplemem_engine(cfg, llm=llm, embed=embed, data_root=str(tmp_path))
    eng.add_dialogues([Dialogue(1, "user", "alpha topic")])
    eng.add_dialogues([Dialogue(1, "user", "beta topic")])
    assert len(prompts) == 2
    assert "JOB-FACT-1" not in prompts[1]   # job 1's output not leaked into job 2's prompt


def test_extraction_failure_raises(tmp_path):
    # Bad JSON on every attempt -> ExtractionFailed (never a silent []), so the
    # generation watermark cannot mistake a failure for "nothing to remember".
    from nanocode.memory.engines.simplemem.errors import ExtractionFailed

    def bad_llm(messages):
        return "not json at all, no array here"

    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM)
    eng = create_simplemem_engine(cfg, llm=bad_llm, embed=embed, data_root=str(tmp_path))
    with pytest.raises(ExtractionFailed):
        eng.add_dialogues([Dialogue(1, "user", "something")])


def test_extraction_missing_field_raises(tmp_path):
    # A JSON array whose item lacks the required lossless_restatement is a failure,
    # not a silently dropped entry (fail-loud _to_entry, docs/21 §7.4).
    from nanocode.memory.engines.simplemem.errors import ExtractionFailed

    def llm(messages):
        return '[{"keywords": ["x"], "topic": "t"}]'   # no lossless_restatement

    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM)
    eng = create_simplemem_engine(cfg, llm=llm, embed=embed, data_root=str(tmp_path))
    with pytest.raises(ExtractionFailed):
        eng.add_dialogues([Dialogue(1, "user", "something")])


def test_extraction_nondict_item_raises(tmp_path):
    # A non-empty array containing a non-dict item (e.g. `[null]`) is a FAILURE,
    # NOT a silently dropped entry that masquerades as a legal empty extraction.
    # Otherwise the generation watermark would advance past an un-extracted batch
    # (the fail-loud hole both reviewers caught).
    from nanocode.memory.engines.simplemem.errors import ExtractionFailed

    embed, _ = make_embedder()
    payloads = ("[null]", '["bare string"]',
                '[{"lossless_restatement": "real fact"}, null]')   # partial junk too
    for i, payload in enumerate(payloads):
        sub = tmp_path / f"s{i}"
        cfg = SimpleMemConfig(root=str(sub / "store"), embed_dimension=EMBED_DIM)
        eng = create_simplemem_engine(cfg, llm=(lambda p: lambda m: p)(payload),
                                      embed=embed, data_root=str(sub))
        with pytest.raises(ExtractionFailed):
            eng.add_dialogues([Dialogue(1, "user", "something")])


def test_extraction_empty_array_is_success(tmp_path):
    # A legal empty array is a success that produces no entries (distinct from a
    # failure): generation may safely advance the watermark on this.
    def llm(messages):
        return "[]"

    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM)
    eng = create_simplemem_engine(cfg, llm=llm, embed=embed, data_root=str(tmp_path))
    assert eng.add_dialogues([Dialogue(1, "user", "nothing worth keeping")]) == []


def _capturing_engine(tmp_path, prompts, *, context_window):
    def llm(messages):
        prompts.append(messages[-1]["content"])
        return ('[{"lossless_restatement": "target fact", "keywords": [], "timestamp": null, '
                '"location": null, "persons": [], "entities": [], "topic": null}]')
    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM,
                          context_window=context_window)
    return create_simplemem_engine(cfg, llm=llm, embed=embed, data_root=str(tmp_path))


def test_context_only_truncates_to_window_and_marks_prompt(tmp_path):
    # docs/21 §13.1: context_dialogues are surfaced under a CONTEXT-ONLY/do-not-extract
    # header and truncated to the last `context_window` turns; the target line stays.
    prompts = []
    eng = _capturing_engine(tmp_path, prompts, context_window=1)
    eng.add_dialogues(
        [Dialogue(1, "user", "TARGET-LINE")],
        context_dialogues=[Dialogue(1, "user", "OLD-CONTEXT-A"),
                           Dialogue(2, "assistant", "OLD-CONTEXT-B")],
    )
    p = prompts[0]
    assert "CONTEXT ONLY" in p and "Do NOT create memory entries" in p
    assert "OLD-CONTEXT-B" in p      # last 1 kept (context_window=1)
    assert "OLD-CONTEXT-A" not in p  # earlier context truncated out
    assert "TARGET-LINE" in p


def test_context_window_zero_disables_context(tmp_path):
    prompts = []
    eng = _capturing_engine(tmp_path, prompts, context_window=0)
    eng.add_dialogues([Dialogue(1, "user", "TARGET")],
                      context_dialogues=[Dialogue(1, "user", "CTX-LINE")])
    assert "CONTEXT ONLY" not in prompts[0]
    assert "CTX-LINE" not in prompts[0]


def test_missing_embed_fails_loud(tmp_path):
    cfg = SimpleMemConfig(root=str(tmp_path / "s"), embed_dimension=EMBED_DIM)
    with pytest.raises(EngineUnavailable):
        create_simplemem_engine(cfg, llm=None, embed=None, data_root=str(tmp_path))


def test_schema_mismatch_raises(tmp_path):
    root = tmp_path / "store"
    root.mkdir(parents=True)
    schema_path(root).write_text(json.dumps({"schema_version": SCHEMA_VERSION + 99}))
    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(root), embed_dimension=EMBED_DIM)
    with pytest.raises(MigrationRequired):
        create_simplemem_engine(cfg, llm=None, embed=embed, data_root=str(tmp_path))


def test_scoped_root_rejects_traversal(tmp_path):
    with pytest.raises(SimpleMemError):
        resolve_scoped_root("../evil", data_root=str(tmp_path))
    with pytest.raises(SimpleMemError):
        resolve_scoped_root("/etc/passwd", data_root=str(tmp_path))


def test_scoped_root_rejects_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "data" / "linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    with pytest.raises(SimpleMemError):
        resolve_scoped_root(str(link), data_root=str(tmp_path / "data"))


def test_scoped_root_accepts_inside(tmp_path):
    resolved = resolve_scoped_root("store/sub", data_root=str(tmp_path))
    assert str(resolved).startswith(str(tmp_path.resolve()))


def test_read_entry_and_list(tmp_path):
    eng, _, _ = _engine(tmp_path)
    e = eng.add_note(MemoryNote(title="T", content="a memorable fact"))
    assert eng.read_entry(e.entry_id).lossless_restatement == "a memorable fact"
    page = eng.list_entries(limit=10)
    assert page.total == 1


# ── regression guards from the docs/20 cross-validation ───────────────
def test_fts_index_is_live_and_lexical_leg_works(tmp_path):
    """Guards the critical FTS regression: native FTS must build and the lexical
    leg must actually return hits (not silently degrade to semantic-only)."""
    eng, _, _ = _engine(tmp_path)
    eng.add_note(MemoryNote(title="K8s", content="rollout uses kubernetes operators"))
    store = eng._store
    assert store.fts_available is True
    hits = store.keyword_search(["kubernetes"], top_k=5)
    assert hits and any("kubernetes" in h.lossless_restatement.lower() for h in hits)


def test_keyword_only_entry_surfaces_via_rrf(tmp_path):
    """An exact keyword hit must reach the top results even amid semantic neighbors."""
    eng, _, _ = _engine(tmp_path)
    eng.add_note(MemoryNote(title="A", content="kubernetes operator reconcile loop"))
    eng.add_note(MemoryNote(title="B", content="the cat sat on the mat"))
    eng.add_note(MemoryNote(title="C", content="weather forecast tomorrow sunny"))
    hits = eng.retrieve_fast("kubernetes", limit=3)
    assert any("kubernetes" in h.lossless_restatement.lower() for h in hits)


def test_semantic_fault_propagates_not_silent_empty(tmp_path):
    """A hard retrieval fault must propagate (docs/20 §2.4 #5: no silent [])."""
    boom = {"q": "__BOOM__"}

    def embed(texts):
        if list(texts) == [boom["q"]]:
            raise RuntimeError("embed endpoint down")
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            for tok in t.lower().split():
                v[sum(ord(c) for c in tok) % EMBED_DIM] += 1.0
            out.append(v)
        return out

    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM)
    eng = create_simplemem_engine(cfg, llm=None, embed=embed, data_root=str(tmp_path))
    eng.add_note(MemoryNote(title="X", content="something"))
    with pytest.raises(RuntimeError):
        eng.retrieve_fast("__BOOM__", limit=5)


def test_structured_search_escapes_apostrophe(tmp_path):
    from nanocode.memory.engines.simplemem.models import MemoryEntry
    eng, _, _ = _engine(tmp_path)
    eng._store.add_entries([MemoryEntry(lossless_restatement="O'Brien shipped it",
                                        persons=["O'Brien"])])
    hits = eng._store.structured_search(persons=["O'Brien"], top_k=5)
    assert hits and hits[0].persons == ["O'Brien"]


def test_corrupt_schema_marker_raises(tmp_path):
    root = tmp_path / "store"
    root.mkdir(parents=True)
    schema_path(root).write_text("{ this is not json")
    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(root), embed_dimension=EMBED_DIM)
    with pytest.raises(MigrationRequired):
        create_simplemem_engine(cfg, llm=None, embed=embed, data_root=str(tmp_path))


def test_unversioned_store_with_data_raises(tmp_path):
    root = tmp_path / "store"
    root.mkdir(parents=True)
    (root / "legacy_table.lance").write_text("old data, no schema marker")
    embed, _ = make_embedder()
    cfg = SimpleMemConfig(root=str(root), embed_dimension=EMBED_DIM)
    with pytest.raises(MigrationRequired):
        create_simplemem_engine(cfg, llm=None, embed=embed, data_root=str(tmp_path))


def test_scoped_root_rejects_symlink_pointing_inside(tmp_path):
    real = tmp_path / "data" / "real"
    real.mkdir(parents=True)
    link = tmp_path / "data" / "link"
    link.symlink_to(real)  # symlink whose target is INSIDE data_root
    with pytest.raises(SimpleMemError):
        resolve_scoped_root(str(link), data_root=str(tmp_path / "data"))


def test_scoped_root_rejects_symlink_via_resolved_alias(tmp_path):
    # data_root is itself a symlink (like macOS /tmp -> /private/tmp); a root
    # spelled through the *resolved* prefix must still hit the symlink-tail walk.
    realbase = tmp_path / "private_d"
    realbase.mkdir()
    data_alias = tmp_path / "d"
    data_alias.symlink_to(realbase)
    (realbase / "ok").mkdir()
    (realbase / "link").symlink_to(realbase / "ok")
    with pytest.raises(SimpleMemError):
        resolve_scoped_root(str(realbase / "link"), data_root=str(data_alias))


def test_structured_only_hit_survives_rrf(tmp_path):
    """A high-precision structured (person) match must reach top-N even when it
    is neither a top semantic neighbor nor a lexical hit (codex round-2 HIGH)."""
    from nanocode.memory.engines.simplemem.models import MemoryEntry
    eng, _, _ = _engine(tmp_path)
    # target: person "Zebra" but restatement has no 'zebra' token (no lexical hit)
    eng._store.add_entries([MemoryEntry(lossless_restatement="quarterly planning notes",
                                        persons=["Zebra"])])
    for i in range(8):
        eng.add_note(MemoryNote(title=f"d{i}", content=f"unrelated decoy chatter number {i}"))
    hits = eng.retrieve_fast("Zebra", limit=5)
    assert any(h.persons == ["Zebra"] for h in hits)
