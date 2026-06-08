"""P2: per-agent session artifacts — always-on wire.jsonl + meta/prompt/result.

Covers:
- main agent gets a real (non-Null) tracer whose wire sink writes agents/main/wire.jsonl
  WITHOUT --trace, and that file contains a session_start line;
- a fresh foreground sub-agent writes meta.json/prompt.txt/result.md/wire.jsonl/messages.json
  into agents/<rec.id>/, and meta flips running->completed;
- a sub-agent's wire.jsonl is SEPARATE from the parent's (events not merged);
- the optional --trace debug sink is ADDITIONALLY attached (and inherited by children),
  while the always-on wire sink stays per-agent;
- wire setup never raises out of __init__ even if the wire dir is unwritable.
"""

import asyncio
import json

import pytest

from nanocode.agent.engine import Agent
from nanocode.session import v2 as _session_v2
from nanocode.trace.tracer import Tracer, NullTracer


def _agent(session_id="p2sid", **kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    kw.setdefault("trace_enabled", False)
    return Agent(api_key="test", session_id=session_id, **kw)


def _read_wire(session_id, agent_id):
    p = _session_v2.agent_wire_path(session_id, agent_id)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _spy_run(parent, *, text="sub done", tokens=None):
    """spy _build_sub_agent: stub run_once to write history + return fixed text/tokens."""
    tokens = tokens or {"input": 5, "output": 2}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            sub._anthropic_messages.append({"role": "user", "content": prompt})
            sub._anthropic_messages.append({"role": "assistant", "content": text})
            # run_once 通常会 emit session_end + close；这里仍调用 tracer 让 wire 落盘。
            sub.tracer.emit("assistant_message", text=text)
            sub.tracer.close()
            return {"text": text, "tokens": tokens}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


# ─── main agent: always-on wire, even without --trace ───────────


def test_main_agent_has_real_tracer_not_null():
    a = _agent()
    assert isinstance(a.tracer, Tracer)
    assert not isinstance(a.tracer, NullTracer)
    assert a.artifact_id == "main"


def test_main_agent_writes_main_wire_with_session_start():
    a = _agent(session_id="mainwire")
    # __init__ already emitted session_start; flush by closing.
    a.tracer.close()
    events = _read_wire("mainwire", "main")
    assert events, "agents/main/wire.jsonl should exist and be non-empty without --trace"
    assert any(e["type"] == "session_start" for e in events)
    # ledger lives under agents/main/, not ./.nanocode/traces
    assert (_session_v2.session_root("mainwire") / "agents" / "main" / "wire.jsonl").exists()


def test_main_wire_present_without_trace_flag():
    # trace_enabled defaults False here; wire is NOT gated by the debug flag.
    a = _agent(session_id="nodbg")
    a.tracer.emit("user_message", text="hi")
    a.tracer.close()
    events = _read_wire("nodbg", "main")
    assert any(e["type"] == "user_message" for e in events)


# ─── fresh foreground sub-agent: full artifact set ──────────────


def test_fresh_subagent_writes_all_artifacts():
    parent = _agent(session_id="fgart")
    _spy_run(parent, text="hello result")
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "build x", "prompt": "the task prompt"}))
    # P3: parent receives a bounded envelope (not the raw transcript). Small text
    # passes through as the summary; the envelope also points at result.md.
    assert "hello result" in res
    assert "result.md" in res

    d = _session_v2.agent_dir("fgart", "agent-001")
    for fname in ("meta.json", "prompt.txt", "result.md", "wire.jsonl", "messages.json"):
        assert (d / fname).exists(), f"missing {fname}"

    assert (d / "prompt.txt").read_text(encoding="utf-8") == "the task prompt"
    # full transcript still on disk verbatim (envelope is bounded; result.md is not)
    assert (d / "result.md").read_text(encoding="utf-8") == "hello result"

    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["id"] == "agent-001"
    assert meta["type"] == "coder"
    assert meta["description"] == "build x"
    assert meta["background"] is False
    assert meta["parent_session_id"] == "fgart"
    assert meta["status"] == "completed"
    assert "created_at" in meta and "ended_at" in meta


def test_fresh_subagent_meta_running_at_spawn():
    """meta.json is written with status=running at spawn (observable mid-flight)."""
    parent = _agent(session_id="spawnmeta")
    real_build = parent._build_sub_agent
    seen = {}

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            # at this point spawn artifacts must already exist with status=running
            seen["meta"] = _session_v2.read_agent_meta("spawnmeta", "agent-001")
            return {"text": "ok", "tokens": {"input": 1, "output": 1}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert seen["meta"]["status"] == "running"


# ─── sub-agent wire is SEPARATE from the parent's wire ──────────


def test_subagent_wire_separate_from_parent():
    parent = _agent(session_id="sepwire")
    _spy_run(parent, text="child text")
    asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "child prompt"}))
    parent.tracer.close()

    parent_events = _read_wire("sepwire", "main")
    child_events = _read_wire("sepwire", "agent-001")

    assert parent_events, "parent (main) wire must exist"
    assert child_events, "child (agent-001) wire must exist"

    # child's session_start landed in the child file...
    assert any(e["type"] == "session_start" for e in child_events)
    # ...and NOT merged into the parent's file (parent has exactly one session_start, its own).
    assert sum(1 for e in parent_events if e["type"] == "session_start") == 1
    # child events carry the parent_session_id link
    assert all(e["parent_session_id"] == "sepwire" for e in child_events)
    # the two wire files are physically distinct
    assert (_session_v2.agent_wire_path("sepwire", "main")
            != _session_v2.agent_wire_path("sepwire", "agent-001"))


# ─── optional --trace debug sink coexists with always-on wire ───


def test_trace_enabled_adds_debug_sink_alongside_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOCODE_TRACE_DIR", str(tmp_path / "traces"))
    a = _agent(session_id="dbg", trace_enabled=True)
    # wire sink (per-agent) + debug sink (./traces) both present
    assert len(a.tracer.sinks) >= 2
    assert len(a.tracer._debug_sinks) == 1
    a.tracer.close()
    # always-on wire still written
    assert _session_v2.agent_wire_path("dbg", "main").exists()
    # debug ledger written too
    assert (tmp_path / "traces" / "dbg.jsonl").exists()


def test_subagent_inherits_parent_debug_sink_but_owns_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOCODE_TRACE_DIR", str(tmp_path / "traces"))
    parent = _agent(session_id="dbgp", trace_enabled=True)
    sub = parent._build_sub_agent(
        system_prompt="s", tools=[], agent_type="coder", artifact_id="agent-001")
    # sub gets its OWN wire sink + the inherited debug sink (not the parent's wire sink)
    assert sub.tracer._debug_sinks == parent.tracer._debug_sinks
    assert sub.tracer.parent_session_id == "dbgp"
    sub.tracer.emit("session_start")
    sub.tracer.close()
    parent.tracer.close()
    # sub wire is its own file
    assert _session_v2.agent_wire_path("dbgp", "agent-001").exists()


def test_subagent_without_debug_has_only_wire_sink():
    parent = _agent(session_id="nodebgp")  # trace_enabled False
    sub = parent._build_sub_agent(
        system_prompt="s", tools=[], agent_type="coder", artifact_id="agent-002")
    assert sub.tracer._debug_sinks == []
    assert len(sub.tracer.sinks) == 1  # wire only


# ─── instrumentation is failure-proof ───────────────────────────


def test_wire_setup_never_raises_when_dir_unwritable(monkeypatch):
    """If agent_wire_path resolution blows up, __init__ must still succeed (no wire)."""
    def _boom(*a, **k):
        raise OSError("unwritable")

    monkeypatch.setattr(_session_v2, "agent_wire_path", _boom)
    # Construction must not raise; tracer is a real Tracer with no wire sink.
    a = _agent(session_id="boomsid")
    assert isinstance(a.tracer, Tracer)
    # emit/close must also be safe (no sinks attached)
    a.tracer.emit("user_message", text="x")
    a.tracer.close()


def test_emit_safe_even_if_wire_file_unwritable(monkeypatch, tmp_path):
    """JsonlSink self-disables on I/O error; emit must never raise."""
    # Point wire at a path whose parent is a *file*, so mkdir/open fails inside the sink.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")

    def _bad_path(session_id, agent_id):
        return blocker / "sub" / "wire.jsonl"

    monkeypatch.setattr(_session_v2, "agent_wire_path", _bad_path)
    a = _agent(session_id="badpath")
    a.tracer.emit("user_message", text="x")  # must not raise
    a.tracer.close()


# ─── P2 cross-review regressions (Codex) ─────────────────────────


def _read_meta(session_id, agent_id):
    return _session_v2.read_agent_meta(session_id, agent_id)


def test_foreground_error_finalizes_meta_and_closes_wire():
    """Codex P2 HIGH: run_once now closes the always-on wire on the error path,
    and the fresh foreground path finalizes meta to 'failed' (not stuck running)."""
    parent = _agent(session_id="fgerr")
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            raise RuntimeError("kaboom")

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert "error" in res.lower()
    meta = _read_meta("fgerr", "agent-001")
    assert meta is not None
    assert meta["status"] == "failed"   # not "running"
    assert meta.get("ended_at")


def test_foreground_cancel_finalizes_meta_cancelled():
    """Codex P2 HIGH: when an outer abort propagates CancelledError out of the
    foreground run helper, the fresh path must finalize meta to 'cancelled' and
    re-raise (not leave meta stuck at 'running'). _await_subagent_run re-raises
    outer cancellation; we simulate that by having the helper raise directly."""
    parent = _agent(session_id="fgcancel")

    async def _cancel(*a, **k):
        raise asyncio.CancelledError()

    parent._run_foreground_subagent = _cancel

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p"}))
    meta = _read_meta("fgcancel", "agent-001")
    assert meta is not None
    assert meta["status"] == "cancelled"
    assert meta.get("ended_at")


def test_foreground_construction_failure_finalizes_meta_failed():
    """Codex P2 MED: if _build_sub_agent raises after spawn artifacts are written,
    the fresh path must finalize meta to 'failed', not leave it 'running'."""
    parent = _agent(session_id="fgbuild")

    def _boom(**kw):
        raise RuntimeError("build failed")

    parent._build_sub_agent = _boom
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert "error" in res.lower()
    meta = _read_meta("fgbuild", "agent-001")
    assert meta is not None
    assert meta["status"] == "failed"


def test_skill_fork_runs_get_separate_wire_ledgers(monkeypatch):
    """Codex P2 MED: each skill-fork invocation gets its OWN agent dir/wire,
    instead of merging into a shared agents/skill-fork/wire.jsonl."""
    def _fake_get_skill(name):
        return None

    def _fake_execute(name, args):
        return {"context": "fork", "prompt": "do it", "allowed_tools": []}

    # _execute_skill_tool does `from ..skills import execute_skill, get_skill_by_name`,
    # so patch the source package namespace (not the engine module).
    monkeypatch.setattr("nanocode.skills.get_skill_by_name", _fake_get_skill, raising=False)
    monkeypatch.setattr("nanocode.skills.execute_skill", _fake_execute, raising=False)

    parent = _agent(session_id="forksep")
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            return {"text": "fork out", "tokens": {"input": 1, "output": 1}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy

    async def scenario():
        await parent._execute_skill_tool({"skill_name": "s1", "args": "a"})
        await parent._execute_skill_tool({"skill_name": "s2", "args": "b"})

    asyncio.run(scenario())
    # two distinct skill-fork records, each with its own agent dir + artifacts
    subs = [s for s in parent.task_manager.list_subagents() if s.type == "skill-fork"]
    assert len(subs) == 2
    dirs = {_session_v2.agent_dir("forksep", s.id) for s in subs}
    assert len(dirs) == 2  # separate dirs, not a shared "skill-fork"
    for s in subs:
        d = _session_v2.agent_dir("forksep", s.id)
        assert (d / "wire.jsonl").exists()
        assert (d / "result.md").exists()
        meta = _read_meta("forksep", s.id)
        assert meta and meta["status"] == "completed"


def test_skill_fork_swallowed_cancel_is_not_marked_completed(monkeypatch):
    """Codex P2 round-2 HIGH: skill-fork went through a bare `await run_once`, so a
    real outer cancel (which chat() swallows) fell into the success path and was
    marked completed. It must instead be detected (via _aborted) and finalized
    cancelled, re-raising CancelledError."""
    def _fake_get_skill(name):
        return None

    def _fake_execute(name, args):
        return {"context": "fork", "prompt": "do it", "allowed_tools": []}

    monkeypatch.setattr("nanocode.skills.get_skill_by_name", _fake_get_skill, raising=False)
    monkeypatch.setattr("nanocode.skills.execute_skill", _fake_execute, raising=False)

    parent = _agent(session_id="forkcancel")
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            # mimic Agent.chat() swallowing an outer CancelledError and returning
            sub._aborted = True
            return {"text": "partial", "tokens": {"input": 1, "output": 1}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(parent._execute_skill_tool({"skill_name": "s", "args": "a"}))
    subs = [s for s in parent.task_manager.list_subagents() if s.type == "skill-fork"]
    assert len(subs) == 1
    assert subs[0].status == "cancelled"   # NOT "completed"
    meta = _read_meta("forkcancel", subs[0].id)
    assert meta and meta["status"] == "cancelled"
