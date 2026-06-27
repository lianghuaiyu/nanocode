import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanocode.memory.jobs import MemoryJobLease
from nanocode.memory.generate import (
    MemoryGenerationPipeline, GenerationEligibility, GenerationResult,
    GenerationTurn, write_extracted_entry_ids,
)
from nanocode.memory.policy import MemoryPolicy, POLLUTED
from nanocode.memory.service import MemoryService, MemoryServiceConfig
from nanocode.memory.engines.simplemem import SimpleMemConfig, create_simplemem_engine, MemoryNote

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


def make_llm():
    calls = {"n": 0}

    def llm(messages):
        calls["n"] += 1
        return ('[{"lossless_restatement": "A durable fact.", "keywords": ["fact"], '
                '"timestamp": null, "location": null, "persons": [], "entities": [], '
                '"topic": "fact"}]')
    return llm, calls


def _engine(tmp_path, *, with_llm=True):
    pytest.importorskip("lancedb")
    llm = make_llm()[0] if with_llm else None
    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=EMBED_DIM)
    return create_simplemem_engine(cfg, llm=llm, embed=make_embedder(), data_root=str(tmp_path))


class FakeEngine:
    """Records the dialogue batches it receives so watermark filtering can be
    asserted precisely (the real engine can't be introspected for inputs)."""

    def __init__(self, *, fail=False, produced_each=1):
        self.batches: list[list[str]] = []          # TARGET contents per add_dialogues call
        self.context_batches: list[list[str]] = []   # context-only contents per call
        self.fail = fail
        self._produced_each = produced_each

    def add_dialogues(self, dialogues, *, context_dialogues=()):
        self.batches.append([d.content for d in dialogues])
        self.context_batches.append([d.content for d in context_dialogues])
        if self.fail:
            raise RuntimeError("extraction boom")
        return ["entry"] * self._produced_each


class FakeStore:
    def __init__(self):
        self.added_batches = []

    def add_entries(self, entries):
        self.added_batches.append(list(entries))


def _read_state(root):
    p = Path(root) / ".generated_entries" / "state.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _assert_lease_released(root):
    # The run() must release the job lease on every exit path (incl. error paths),
    # or later generation for this store would deadlock-skip forever.
    lease = MemoryJobLease.acquire(root)
    assert lease is not None, "generation lease was not released"
    lease.release()


def _turn(eid, speaker, content):
    return GenerationTurn(entry_id=eid, speaker=speaker, content=content, timestamp=None)


_TURNS = [_turn("e1", "user", "how do we deploy"), _turn("e2", "assistant", "use the fleet pipeline")]
_ELIGIBLE = GenerationEligibility(is_root=True, is_subagent=False, ephemeral=False)


# ── lease contention ──────────────────────────────────────────────
def test_lease_is_exclusive(tmp_path):
    a = MemoryJobLease.acquire(str(tmp_path))
    assert a is not None
    b = MemoryJobLease.acquire(str(tmp_path))
    assert b is None  # second worker cannot acquire
    a.release()
    c = MemoryJobLease.acquire(str(tmp_path))
    assert c is not None  # released -> reacquirable
    c.release()


def test_pipeline_skips_when_lease_held(tmp_path):
    eng = _engine(tmp_path)
    held = MemoryJobLease.acquire(eng.stats()["root"])
    try:
        res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
            _TURNS, eligibility=_ELIGIBLE, lease_root=eng.stats()["root"])
        assert not res.ran and "lease" in res.skipped_reason
        # contention is "not run", never "generated": watermark untouched so a
        # later trigger re-extracts these turns.
        assert _read_state(eng.stats()["root"]) is None
    finally:
        held.release()


def test_lease_bounded_retry_acquires_after_release(tmp_path):
    # docs/21 §13.2 / D5: a bounded retry lets a near-simultaneous teardown race
    # resolve — the loser acquires once the winner releases, instead of dropping.
    import threading
    held = MemoryJobLease.acquire(str(tmp_path))
    assert held is not None
    threading.Timer(0.1, held.release).start()
    got = MemoryJobLease.acquire(str(tmp_path), timeout=1.0, poll=0.02)
    assert got is not None   # acquired after the holder released, within the deadline
    got.release()


def test_lease_bounded_retry_gives_up_after_deadline(tmp_path):
    # The retry is bounded: a lease held past the deadline still yields None.
    held = MemoryJobLease.acquire(str(tmp_path))
    try:
        got = MemoryJobLease.acquire(str(tmp_path), timeout=0.1, poll=0.02)
        assert got is None
    finally:
        held.release()


def test_pipeline_lease_timeout_lost_race_is_not_generated(tmp_path):
    # docs/21 §13.2 / D5: with a bounded lease_timeout, a race lost past the deadline
    # is still "not run" — engine never called, watermark untouched, retriable later.
    eng = FakeEngine()
    root = str(tmp_path / "store")
    held = MemoryJobLease.acquire(root)
    try:
        res = MemoryGenerationPipeline(eng, MemoryPolicy(), lease_timeout=0.1).run(
            _TURNS, eligibility=_ELIGIBLE, lease_root=root)
        assert not res.ran and "lease" in res.skipped_reason
        assert eng.batches == []            # engine never called on a lost race
        assert _read_state(root) is None    # watermark untouched
    finally:
        held.release()


# ── eligibility + policy gates ────────────────────────────────────
def test_subagent_session_skipped(tmp_path):
    eng = _engine(tmp_path)
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=GenerationEligibility(True, True, False),
        lease_root=eng.stats()["root"])
    assert not res.ran and "sub-agent" in res.skipped_reason


def test_non_root_session_skipped(tmp_path):
    eng = _engine(tmp_path)
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=GenerationEligibility(False, False, False),
        lease_root=eng.stats()["root"])
    assert not res.ran and "non-root" in res.skipped_reason


def test_ephemeral_session_skipped(tmp_path):
    eng = _engine(tmp_path)
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=GenerationEligibility(True, False, True),
        lease_root=eng.stats()["root"])
    assert not res.ran and "ephemeral" in res.skipped_reason


def test_polluted_thread_skips_generation(tmp_path):
    eng = _engine(tmp_path)
    res = MemoryGenerationPipeline(eng, MemoryPolicy(mode=POLLUTED)).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=eng.stats()["root"])
    assert not res.ran and "polluted" in res.skipped_reason.lower()


def test_generate_disabled_skips(tmp_path):
    eng = _engine(tmp_path)
    res = MemoryGenerationPipeline(eng, MemoryPolicy(generate_memories=False)).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=eng.stats()["root"])
    assert not res.ran


# ── happy path + entry-id watermark ───────────────────────────────
def test_eligible_generation_produces_entries(tmp_path):
    eng = _engine(tmp_path)
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=eng.stats()["root"])
    assert res.ran and res.produced >= 1
    assert eng.stats()["count"] >= 1


def test_generation_records_extracted_entry_ids(tmp_path):
    eng = _engine(tmp_path)
    root = eng.stats()["root"]
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert res.ran and res.produced >= 1
    state = _read_state(root)
    assert state["schema"] == 1
    assert state["extracted_entry_ids"] == ["e1", "e2"]   # sorted, stable


def test_resume_extend_generates_only_new_entry_ids(tmp_path):
    eng = FakeEngine()
    root = str(tmp_path / "store")
    pipe = MemoryGenerationPipeline(eng, MemoryPolicy())
    first = pipe.run(_TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert first.ran
    extended = _TURNS + [_turn("e3", "user", "and rollback"), _turn("e4", "assistant", "fleet rollback cmd")]
    second = pipe.run(extended, eligibility=_ELIGIBLE, lease_root=root)
    assert second.ran
    # first job saw e1/e2; the resume job saw ONLY the new e3/e4.
    assert eng.batches[0] == ["how do we deploy", "use the fleet pipeline"]
    assert eng.batches[1] == ["and rollback", "fleet rollback cmd"]
    assert _read_state(root)["extracted_entry_ids"] == ["e1", "e2", "e3", "e4"]


def test_resume_extend_passes_preceding_turns_as_context(tmp_path):
    # docs/21 §13.1: the resume job extracts ONLY the new turns, but the already-
    # extracted turns immediately preceding them are passed as read-only context
    # (pronoun/antecedent resolution) — never re-extracted or watermarked.
    eng = FakeEngine()
    root = str(tmp_path / "store")
    pipe = MemoryGenerationPipeline(eng, MemoryPolicy())
    pipe.run(_TURNS, eligibility=_ELIGIBLE, lease_root=root)
    extended = _TURNS + [_turn("e3", "user", "and rollback"), _turn("e4", "assistant", "fleet rollback cmd")]
    pipe.run(extended, eligibility=_ELIGIBLE, lease_root=root)
    # first job: targets e1/e2, nothing precedes them.
    assert eng.batches[0] == ["how do we deploy", "use the fleet pipeline"]
    assert eng.context_batches[0] == []
    # resume job: targets ONLY e3/e4; e1/e2 handed over as read-only context.
    assert eng.batches[1] == ["and rollback", "fleet rollback cmd"]
    assert eng.context_batches[1] == ["how do we deploy", "use the fleet pipeline"]


def test_force_passes_no_context(tmp_path):
    # force re-extracts the whole branch (first_new_idx == 0) → no separate context.
    eng = FakeEngine()
    root = str(tmp_path / "store")
    write_extracted_entry_ids(root, {"e1", "e2"})
    branch = [_turn("e1", "user", "a"), _turn("e2", "assistant", "b"), _turn("e3", "user", "c")]
    MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        branch, eligibility=_ELIGIBLE, lease_root=root, force=True)
    assert eng.batches[0] == ["a", "b", "c"]
    assert eng.context_batches[0] == []


def test_no_new_turns_skips_cleanly(tmp_path):
    eng = FakeEngine()
    root = str(tmp_path / "store")
    pipe = MemoryGenerationPipeline(eng, MemoryPolicy())
    pipe.run(_TURNS, eligibility=_ELIGIBLE, lease_root=root)
    res = pipe.run(_TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert res.ran is False
    assert res.skipped_reason == "no new turns since last generation"
    assert len(eng.batches) == 1   # engine not called again


def test_legal_empty_extraction_still_writes_watermark(tmp_path):
    # A legal empty extraction (engine returns []) is a SUCCESS: the watermark
    # must still advance so the same turns aren't re-extracted forever. Distinct
    # from a failure, which must NOT advance it (docs/21 §6 rule 4).
    eng = FakeEngine(produced_each=0)
    root = str(tmp_path / "store")
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert res.ran is True and res.produced == 0
    assert _read_state(root)["extracted_entry_ids"] == ["e1", "e2"]   # watermark advanced
    # ... and a re-run cleanly skips (no infinite re-extraction of empty turns).
    res2 = MemoryGenerationPipeline(FakeEngine(produced_each=0), MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert res2.ran is False and res2.skipped_reason == "no new turns since last generation"


def test_clone_preserving_ids_does_not_reextract(tmp_path):
    # clone/fork copy entries verbatim, preserving ids (session/manager.clone).
    # The store-level watermark must dedup by id so the cloned session does not
    # re-extract — and so the shared index gains no duplicate (docs/21 §5/D2).
    eng = FakeEngine()
    root = str(tmp_path / "store")
    pipe = MemoryGenerationPipeline(eng, MemoryPolicy())
    pipe.run(_TURNS, eligibility=_ELIGIBLE, lease_root=root)
    cloned_branch = [_turn("e1", "user", "how do we deploy"),
                     _turn("e2", "assistant", "use the fleet pipeline")]
    res = pipe.run(cloned_branch, eligibility=_ELIGIBLE, lease_root=root)
    assert res.ran is False and res.skipped_reason == "no new turns since last generation"
    assert len(eng.batches) == 1   # no duplicate extraction across the clone


def test_force_reextracts_and_unions_into_store_ids(tmp_path):
    # force ignores the filter (re-extracts the whole current branch, accepting
    # duplicate index entries) but only UNIONS branch ids into the watermark — it
    # must never clear ids from other branches (docs/21 §5/D2 correction).
    eng = FakeEngine()
    root = str(tmp_path / "store")
    write_extracted_entry_ids(root, {"e1", "e2", "e_old_other_branch"})
    branch = [_turn("e1", "user", "a"), _turn("e2", "assistant", "b"), _turn("e3", "user", "c")]
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        branch, eligibility=_ELIGIBLE, lease_root=root, force=True)
    assert res.ran
    assert eng.batches[0] == ["a", "b", "c"]   # whole branch re-extracted
    assert _read_state(root)["extracted_entry_ids"] == ["e1", "e2", "e3", "e_old_other_branch"]


# ── engine commit boundary: extraction is all-or-nothing per job ─────
def _pure_engine(llm, *, window_size=2, overlap_size=0):
    from nanocode.memory.engines.simplemem import SimpleMemEngine, SimpleMemConfig
    from nanocode.memory.engines.simplemem.embeddings import Embedder
    from nanocode.memory.engines.simplemem.llm import LlmClient

    store = FakeStore()
    eng = SimpleMemEngine(
        SimpleMemConfig(root="/unused", embed_dimension=EMBED_DIM,
                        window_size=window_size, overlap_size=overlap_size),
        llm=LlmClient(llm),
        embedder=Embedder(lambda texts: [[0.0] * EMBED_DIM for _ in texts], EMBED_DIM),
        store=store,
    )
    return eng, store


def _entry_json(text):
    return json.dumps([{
        "lossless_restatement": text,
        "keywords": [],
        "timestamp": None,
        "location": None,
        "persons": [],
        "entities": [],
        "topic": None,
    }])


def test_engine_commits_once_after_all_windows_succeed():
    from nanocode.memory.engines.simplemem import Dialogue

    calls = {"n": 0}

    def llm(messages):
        calls["n"] += 1
        return _entry_json(f"window {calls['n']} fact")

    eng, store = _pure_engine(llm, window_size=2, overlap_size=0)
    produced = eng.add_dialogues([
        Dialogue(1, "user", "a"),
        Dialogue(2, "assistant", "b"),
        Dialogue(3, "user", "c"),
    ])
    assert len(produced) == 2
    assert len(store.added_batches) == 1
    assert [e.lossless_restatement for e in store.added_batches[0]] == [
        "window 1 fact", "window 2 fact",
    ]


def test_engine_does_not_partial_write_when_later_window_fails():
    from nanocode.memory.engines.simplemem import Dialogue
    from nanocode.memory.engines.simplemem.errors import ExtractionFailed

    calls = {"n": 0}

    def llm(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return _entry_json("first window fact")
        return "not json"

    eng, store = _pure_engine(llm, window_size=2, overlap_size=0)
    with pytest.raises(ExtractionFailed):
        eng.add_dialogues([
            Dialogue(1, "user", "a"),
            Dialogue(2, "assistant", "b"),
            Dialogue(3, "user", "c"),
        ])
    assert store.added_batches == []


# ── failure isolation: failure never advances the watermark ───────
def test_worker_failure_preserves_existing_index(tmp_path):
    # engine with no llm -> add_dialogues fails loud; pre-existing entry survives.
    eng = _engine(tmp_path, with_llm=False)
    eng.add_note(MemoryNote(title="keep", content="precious existing memory"))
    before = eng.stats()["count"]
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=eng.stats()["root"])
    assert res.ran and res.error and res.produced == 0
    assert eng.stats()["count"] == before  # index untouched on worker failure


def test_generation_failure_does_not_write_watermark(tmp_path):
    eng = FakeEngine(fail=True)
    root = str(tmp_path / "store")
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert res.ran is True and res.error and res.produced == 0
    # watermark NOT written -> a later run retries the same batch.
    assert _read_state(root) is None
    # and the retry actually re-extracts the same ids (no progress was burned).
    eng_ok = FakeEngine()
    retry = MemoryGenerationPipeline(eng_ok, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert retry.ran and eng_ok.batches[0] == ["how do we deploy", "use the fleet pipeline"]


def test_malformed_state_is_observable_error(tmp_path):
    eng = FakeEngine()
    root = str(tmp_path / "store")
    d = Path(root) / ".generated_entries"
    d.mkdir(parents=True)
    (d / "state.json").write_text("{ this is not valid json", encoding="utf-8")
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert res.ran is False and res.error
    assert eng.batches == []   # engine never called on a corrupt watermark
    _assert_lease_released(root)


def test_unknown_schema_state_is_observable_error(tmp_path):
    eng = FakeEngine()
    root = str(tmp_path / "store")
    d = Path(root) / ".generated_entries"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"schema": 999, "extracted_entry_ids": []}),
                                  encoding="utf-8")
    res = MemoryGenerationPipeline(eng, MemoryPolicy()).run(
        _TURNS, eligibility=_ELIGIBLE, lease_root=root)
    assert res.ran is False and res.error
    assert eng.batches == []
    _assert_lease_released(root)


# ── service integration ───────────────────────────────────────────
class FakeMgr:
    def __init__(self, branch, parent=None):
        self._branch = branch
        self._parent = parent

    def get_branch(self):
        return self._branch

    def spawned_by(self):
        return self._parent

    def forked_from(self):
        return None


class FakeStatsEngine:
    def stats(self):
        return {"root": "/unused"}

    def add_dialogues(self, dialogues, *, context_dialogues=()):
        raise AssertionError("engine should not be called after a session read failure")


class FakeSimplememBackend:
    name = "simplemem"
    engine = FakeStatsEngine()

    def stats(self):
        return {"backend": "simplemem", "root": "/unused"}


def _entry(role, text, eid):
    from nanocode.session import tree as _tree
    return SimpleNamespace(type=_tree.MESSAGE, id=eid, timestamp=None,
                           data={"message": {"role": role, "content": text}})


def test_service_generation_markdown_backend_skips():
    svc = MemoryService(config=MemoryServiceConfig(backend="markdown"), cwd=".", agent_dir=".")
    mgr = FakeMgr([_entry("user", "hi", "e1"), _entry("assistant", "yo", "e2")])
    res = asyncio.run(svc.maybe_start_generation_pipeline(thread_id="t", session_mgr=mgr))
    assert not res.ran and "markdown" in res.skipped_reason


def test_service_generation_get_branch_failure_is_error():
    class BrokenMgr(FakeMgr):
        def get_branch(self):
            raise RuntimeError("branch blew up")

    svc = MemoryService(config=MemoryServiceConfig(backend="markdown"), cwd=".", agent_dir=".")
    svc._backend = FakeSimplememBackend()
    res = asyncio.run(svc.maybe_start_generation_pipeline(thread_id="t", session_mgr=BrokenMgr([])))
    assert res.ran is False and res.error and "branch blew up" in res.error
    assert res.skipped_reason is None


def test_service_generation_parent_session_failure_is_error():
    class BrokenMgr(FakeMgr):
        def spawned_by(self):
            raise RuntimeError("parent blew up")

    svc = MemoryService(config=MemoryServiceConfig(backend="markdown"), cwd=".", agent_dir=".")
    svc._backend = FakeSimplememBackend()
    mgr = BrokenMgr([_entry("user", "hi", "e1")])
    res = asyncio.run(svc.maybe_start_generation_pipeline(thread_id="t", session_mgr=mgr))
    assert res.ran is False and res.error and "parent blew up" in res.error
    assert res.skipped_reason is None


def test_service_generation_simplemem_runs(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_DIM", str(EMBED_DIM))
    svc = MemoryService(config=MemoryServiceConfig(backend="simplemem"),
                        cwd=str(tmp_path), agent_dir=str(tmp_path),
                        llm=make_llm()[0], embed=(make_embedder(), EMBED_DIM))
    mgr = FakeMgr([_entry("user", "how do we deploy", "e1"), _entry("assistant", "fleet", "e2")])
    res = asyncio.run(svc.maybe_start_generation_pipeline(thread_id="t", session_mgr=mgr))
    assert res.ran and res.produced >= 1
    # resume-extend idempotency: same branch (same entry ids) -> no new turns.
    res2 = asyncio.run(svc.maybe_start_generation_pipeline(thread_id="t", session_mgr=mgr))
    assert not res2.ran and res2.skipped_reason == "no new turns since last generation"
    # force re-runs
    res3 = asyncio.run(svc.maybe_start_generation_pipeline(thread_id="t", session_mgr=mgr, force=True))
    assert res3.ran
