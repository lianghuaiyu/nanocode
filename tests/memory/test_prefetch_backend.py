import asyncio
from nanocode.memory import recall
from nanocode.memory.recall import RelevantMemory


class FakeSimpleMemBackend:
    name = "simplemem"
    def __init__(self, hits): self._hits = hits
    def retrieve(self, query, *, limit=5, token_budget=0):
        return self._hits


def test_simplemem_prefetch_returns_hits():
    hits = [RelevantMemory(path="simplemem://1", content="c", mtime_ms=0, header="H")]
    b = FakeSimpleMemBackend(hits)
    out = asyncio.run(recall._simplemem_prefetch("multi word query", b, set()))
    assert len(out) == 1 and out[0].path == "simplemem://1"


def test_simplemem_prefetch_filters_already_surfaced():
    hits = [
        RelevantMemory(path="simplemem://1", content="c", mtime_ms=0, header="H"),
        RelevantMemory(path="simplemem://2", content="d", mtime_ms=0, header="H"),
    ]
    out = asyncio.run(recall._simplemem_prefetch("q", FakeSimpleMemBackend(hits), {"simplemem://1"}))
    assert [h.path for h in out] == ["simplemem://2"]


def test_start_prefetch_simplemem_path():
    async def go():
        b = FakeSimpleMemBackend(
            [RelevantMemory(path="simplemem://1", content="c", mtime_ms=0, header="H")]
        )
        pf = recall.start_memory_prefetch(
            "two words", lambda s, u: "", set(), 0, backend=b,
        )
        assert pf is not None
        res = await pf.task
        assert len(res) == 1
    asyncio.run(go())


def test_start_prefetch_markdown_backend_unchanged():
    # markdown 后端 + 无 md 文件 → 仍走旧逻辑 gate → None（与现状一致）
    class FakeMd:
        name = "markdown"
    pf = recall.start_memory_prefetch("two words", lambda s, u: "", set(), 0, backend=FakeMd())
    assert pf is None


def test_start_prefetch_none_backend_is_legacy():
    # backend=None（旧调用）单词 → None，与现状一致
    assert recall.start_memory_prefetch("word", lambda s, u: "", set(), 0) is None
