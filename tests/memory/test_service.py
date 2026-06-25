import asyncio

import pytest

from nanocode.memory.service import MemoryService, MemoryServiceConfig


class FakeHost:
    def __init__(self, is_sub_agent=False):
        self.is_sub_agent = is_sub_agent
        self.consolidated = False

    async def spawn_memory_consolidate(self):
        self.consolidated = True
        return "consolidation started"


def _svc(backend, **cfg):
    return MemoryService(config=MemoryServiceConfig(backend=backend, **cfg),
                         cwd=".", agent_dir=".")


# ── off backend ───────────────────────────────────────────────────
def test_off_static_prompt_empty():
    assert _svc("off").static_prompt() == ""


def test_off_prefetch_none():
    assert _svc("off").start_prefetch("a b c", already_surfaced=set(),
                                      session_memory_bytes=0) is None


def test_off_tool_disabled():
    out = asyncio.run(_svc("off").execute_tool({"action": "search", "query": "x"},
                                               host=FakeHost()))
    assert "off" in out.lower()


# ── markdown backend ──────────────────────────────────────────────
def test_markdown_static_prompt_is_file_based():
    p = _svc("markdown").static_prompt()
    assert "file-based memory system" in p


def test_markdown_tool_add_search_read_list_stats():
    svc = _svc("markdown")
    host = FakeHost()
    add = asyncio.run(svc.execute_tool(
        {"action": "add_note", "title": "Deploy", "kind": "project",
         "content": "deploy via fleet"}, host=host))
    assert "explicit" in add.lower()
    search = asyncio.run(svc.execute_tool({"action": "search", "query": "deploy"}, host=host))
    assert "deploy" in search.lower()
    listed = asyncio.run(svc.execute_tool({"action": "list"}, host=host))
    assert "Deploy" in listed
    stats = asyncio.run(svc.execute_tool({"action": "stats"}, host=host))
    assert "markdown" in stats


def test_markdown_unknown_action():
    out = asyncio.run(_svc("markdown").execute_tool({"action": "frobnicate"}, host=FakeHost()))
    assert "Unknown memory action" in out


def test_consolidate_blocked_for_subagent():
    out = asyncio.run(_svc("markdown").execute_tool(
        {"action": "consolidate"}, host=FakeHost(is_sub_agent=True)))
    assert "sub-agent" in out.lower()


def test_consolidate_delegates_to_host():
    host = FakeHost()
    out = asyncio.run(_svc("markdown").execute_tool({"action": "consolidate"}, host=host))
    assert host.consolidated and "consolidation started" in out


def test_use_disabled_blocks_tool():
    out = asyncio.run(_svc("markdown", use_memories=False).execute_tool(
        {"action": "search", "query": "x"}, host=FakeHost()))
    assert "disabled" in out.lower()


# ── simplemem unavailable: explicit error, never markdown fallback ─
def test_simplemem_without_embed_fails_loud():
    with pytest.raises(Exception):
        MemoryService(config=MemoryServiceConfig(backend="simplemem"),
                      cwd=".", agent_dir=".")  # no embed callable -> loud failure


# ── policy passthrough ────────────────────────────────────────────
def test_external_context_marks_polluted():
    svc = _svc("markdown")
    assert svc.policy.allows_generation
    assert svc.on_external_context_used(source="web_fetch") is True
    assert not svc.policy.allows_generation


# ── store faults surface (docs/20 §2.4 #5: no silent []) ──────────
def test_search_surfaces_backend_fault():
    svc = _svc("markdown")

    class Raising:
        name = "markdown"
        def retrieve_fast(self, q, *, limit, token_budget):
            raise RuntimeError("backend exploded")

    svc._backend = Raising()
    out = asyncio.run(svc.execute_tool({"action": "search", "query": "x"}, host=FakeHost()))
    assert "search failed" in out and "backend exploded" in out


def test_stats_surfaces_backend_fault():
    svc = _svc("markdown")

    class Raising:
        name = "markdown"
        def stats(self):
            raise RuntimeError("count blew up")

    svc._backend = Raising()
    out = asyncio.run(svc.execute_tool({"action": "stats"}, host=FakeHost()))
    assert "stats failed" in out and "count blew up" in out


# ── generation transcript projection (docs/21) ─────────────────────
def test_turns_from_session_carries_entry_id(tmp_path):
    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager
    from nanocode.memory.generate import GenerationTurn

    mgr = SessionManager.create(T.new_id("sess"), cwd=str(tmp_path))
    try:
        u = mgr.append_message(T.user_message("remember alpha"))
        a = mgr.append_message({"role": "assistant", "content": "noted alpha"})
        turns = _svc("markdown")._turns_from_session(mgr)
    finally:
        mgr.close()
    assert all(isinstance(t, GenerationTurn) for t in turns)
    by_id = {t.entry_id: t for t in turns}
    assert by_id[u.id].speaker == "user" and by_id[u.id].content == "remember alpha"
    assert by_id[a.id].speaker == "assistant" and by_id[a.id].content == "noted alpha"


def test_turns_from_session_uses_raw_branch_not_folded_context(tmp_path):
    # Generation input is the RAW branch (get_branch), never build_context()
    # which folds compaction summaries into LLM context (docs/21 §12.1).
    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager

    mgr = SessionManager.create(T.new_id("sess"), cwd=str(tmp_path))
    try:
        mgr.append_message(T.user_message("old user question"))
        mgr.append_message({"role": "assistant", "content": "old assistant answer"})
        mgr.append_compaction(summary="FOLDED SUMMARY OF OLD")
        mgr.append_message(T.user_message("new user question"))
        turns = _svc("markdown")._turns_from_session(mgr)
    finally:
        mgr.close()
    contents = [t.content for t in turns]
    assert "old user question" in contents      # raw user/assistant entries survive
    assert "old assistant answer" in contents
    assert "new user question" in contents
    assert all("FOLDED SUMMARY OF OLD" not in c for c in contents)   # summary excluded
    assert all(t.speaker in ("user", "assistant") for t in turns)


