"""P3: 结构化 AgentResult + 有界注入信封（engine.py）。

覆盖：
- _on_file_touched 把 read 记入 _files_read、write/edit 记入 _files_modified；
- build_agent_result 取子 agent 观测的文件事实 + 解析 summary/findings（docs/16 #7b：engine 委托 shim 已删，直测纯函数）；
- render_agent_result_envelope：小文本直通；大文本截断 + result_path 指针；
  files_modified 出现在信封；findings cap；files cap + 溢出计数；
- 前台 tool_result 是有界信封（含 summary + result_path），不是整段大 transcript。
"""

import asyncio
import json

from nanocode.agent.engine import Agent
from nanocode.agent.agent_result import build_agent_result, render_agent_result_envelope
from nanocode.session import v2 as _session_v2


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="p3sid", **kw)


# ─── host-derived file tracking ─────────────────────────────


def test_on_file_touched_records_read_vs_modified():
    a = _agent()
    a._on_file_touched("read_file", {"file_path": "alpha.py"})
    a._on_file_touched("write_file", {"file_path": "beta.py"})
    a._on_file_touched("edit_file", {"file_path": "gamma.py"})

    reads = {p.split("/")[-1] for p in a._files_read}
    mods = {p.split("/")[-1] for p in a._files_modified}
    assert reads == {"alpha.py"}
    assert mods == {"beta.py", "gamma.py"}


def test_on_file_touched_ignores_missing_path():
    a = _agent()
    a._on_file_touched("read_file", {})  # no file_path → no-op, no raise
    assert a._files_read == set()
    assert a._files_modified == set()


# ─── _build_agent_result ────────────────────────────────────


def test_build_agent_result_uses_subagent_observed_files():
    parent = _agent()
    sub = parent._build_sub_agent(
        system_prompt="s", tools=[], agent_type="coder")
    sub._files_read.add("/repo/read_one.py")
    sub._files_modified.add("/repo/wrote_one.py")

    text = "```agent-result\n{\"summary\": \"did it\", \"findings\": [\"f1\"]}\n```"
    r = build_agent_result(sub, text, {"input": 10, "output": 3}, "/s/result.md")

    assert r["summary"] == "did it"
    assert r["findings"] == ["f1"]
    # host-derived from the SUB-AGENT's observed sets
    assert r["files_read"] == ["/repo/read_one.py"]
    assert r["files_modified"] == ["/repo/wrote_one.py"]
    assert r["tokens"] == {"input": 10, "output": 3}
    assert r["result_path"] == "/s/result.md"


def test_build_agent_result_does_not_trust_model_file_claims():
    """Even if the model fabricates files in its text, files_* come from observation."""
    parent = _agent()
    sub = parent._build_sub_agent(system_prompt="s", tools=[], agent_type="coder")
    # model text claims to have touched files, but sub observed nothing
    text = "I modified /etc/passwd and read /secret. (lies)"
    r = build_agent_result(sub, text, {"input": 1, "output": 1}, None)
    assert r["files_read"] == []
    assert r["files_modified"] == []


# ─── _render_agent_result_envelope ──────────────────────────


def test_envelope_small_text_passes_through():
    parent = _agent()
    result = {
        "summary": "ignored when small", "findings": [],
        "files_read": [], "files_modified": [],
        "tokens": {"input": 1, "output": 1}, "result_path": "/s/result.md",
    }
    raw = "A concise explore deliverable that fits."
    env = render_agent_result_envelope(result, raw)
    assert "A concise explore deliverable that fits." in env
    assert "/s/result.md" in env
    assert "truncated" not in env


def test_envelope_large_text_truncates_with_pointer():
    parent = _agent()
    big = "B" * 9000  # > 4KB passthrough threshold
    result = {
        "summary": "Short model summary", "findings": [],
        "files_read": [], "files_modified": [],
        "tokens": {"input": 5, "output": 2}, "result_path": "/s/result.md",
    }
    env = render_agent_result_envelope(result, big)
    # raw transcript must NOT be dumped wholesale
    assert big not in env
    assert "B" * 5000 not in env
    assert "Short model summary" in env
    assert "truncated" in env
    assert "/s/result.md" in env
    assert "read_file" in env


def test_envelope_includes_files_modified():
    parent = _agent()
    result = {
        "summary": "s", "findings": [],
        "files_read": ["/r/a.py", "/r/b.py"],
        "files_modified": ["/r/changed.py"],
        "tokens": {"input": 1, "output": 1}, "result_path": "/s/result.md",
    }
    env = render_agent_result_envelope(result, "small body")
    assert "Files modified:" in env
    assert "/r/changed.py" in env
    assert "Files read: 2" in env


def test_envelope_caps_findings_and_files_with_overflow():
    parent = _agent()
    result = {
        "summary": "s",
        "findings": [f"finding-{i}" for i in range(25)],
        "files_read": [],
        "files_modified": [f"/r/file-{i}.py" for i in range(25)],
        "tokens": {"input": 1, "output": 1}, "result_path": "/s/r.md",
    }
    env = render_agent_result_envelope(result, "small body")
    # findings capped at 10 + overflow line
    assert "finding-0" in env and "finding-9" in env
    assert "finding-10" not in env
    assert "+15 more" in env  # both findings and files overflow by 15
    # files capped at 10 + overflow
    assert "/r/file-0.py" in env and "/r/file-9.py" in env
    assert "/r/file-10.py" not in env


def test_envelope_large_text_no_result_path_pointer():
    parent = _agent()
    big = "C" * 9000
    result = {
        "summary": "model summary", "findings": [],
        "files_read": [], "files_modified": [],
        "tokens": {"input": 0, "output": 0}, "result_path": None,
    }
    env = render_agent_result_envelope(result, big)
    assert big not in env
    assert "not persisted" in env


# ─── foreground integration: envelope replaces raw transcript ──


def test_foreground_returns_envelope_not_raw_large_transcript():
    parent = _agent()
    huge = "Z" * 12000  # large deliverable — must NOT be dumped raw into parent

    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            return {"text": huge, "tokens": {"input": 4, "output": 6}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))

    # bounded: raw transcript not dumped; pointer + result_path present
    assert huge not in res
    assert "Z" * 5000 not in res
    assert "result.md" in res
    assert "truncated" in res
    # full transcript still recoverable on disk
    from nanocode.session import v2 as _v2
    assert (_v2.agent_dir("p3sid", "agent-001") / "result.md").read_text(
        encoding="utf-8") == huge
    # SubAgentRecord.last_result_path populated
    rec = parent.task_manager.get_subagent("agent-001")
    assert rec.last_result_path and rec.last_result_path.endswith("result.md")


def test_foreground_envelope_surfaces_structured_findings():
    parent = _agent()
    body = (
        "Done.\n```agent-result\n"
        '{"summary": "Implemented feature", "findings": ["edge case A", "perf B"]}\n'
        "```\n"
    ) + ("padding " * 800)  # push over passthrough so summary path is used

    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            return {"text": body, "tokens": {"input": 1, "output": 1}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert "Implemented feature" in res
    assert "edge case A" in res
    assert "perf B" in res
    assert "result.md" in res


# ─── P3 review (reviewer + Codex) regressions ───────────────────


def test_skill_fork_large_output_is_bounded_envelope(monkeypatch):
    """Reviewer P3 MED: skill-fork foreground must return the bounded envelope,
    not the raw transcript, and set last_result_path."""
    def _fake_get_skill(name):
        return None

    def _fake_execute(name, args):
        return {"context": "fork", "prompt": "do it", "allowed_tools": []}

    monkeypatch.setattr("nanocode.skills.get_skill_by_name", _fake_get_skill, raising=False)
    monkeypatch.setattr("nanocode.skills.execute_skill", _fake_execute, raising=False)

    huge = "Z" * 20000
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            return {"text": huge, "tokens": {"input": 1, "output": 1}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    res = asyncio.run(parent._execute_skill_tool({"skill_name": "s", "args": "a"}))
    assert len(res) < 6000              # bounded envelope, NOT the 20KB transcript
    assert huge not in res              # raw transcript not dumped
    assert "result.md" in res          # points to the on-disk full result
    subs = [s for s in parent.task_manager.list_subagents() if s.type == "skill-fork"]
    assert subs and subs[0].last_result_path and subs[0].last_result_path.endswith("result.md")
    # full transcript recoverable on disk
    d = _session_v2.agent_dir("p3sid", subs[0].id)
    assert (d / "result.md").read_text(encoding="utf-8") == huge


def test_envelope_caps_verbose_model_summary():
    """Reviewer P3 MED: a model-authored summary (fenced agent-result) must be
    length-capped so the envelope body is bounded regardless of model cooperation."""
    from nanocode.subagents.result import parse_structured_result, SUMMARY_MAX_CHARS
    big_summary = "S" * 12000
    text = "preamble\n```agent-result\n" + json.dumps({"summary": big_summary, "findings": []}) + "\n```\n"
    parsed = parse_structured_result(text)
    assert len(parsed["summary"]) <= SUMMARY_MAX_CHARS + 40   # cap + ellipsis marker
    assert "summary truncated" in parsed["summary"]


def test_empty_output_envelope_does_not_say_truncated():
    """Reviewer P3 LOW: empty sub-agent output should not claim '(truncated)'."""
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            return {"text": "", "tokens": {"input": 0, "output": 0}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p"}))
    assert "truncated" not in res
    assert "no output" in res.lower()


# ─── Holistic review HIGH: terminal (timeout/error) cost + breadcrumb ──


def test_timed_out_subagent_folds_tokens_into_parent():
    """Cost safety: a sub-agent that spends tokens then times out must still fold
    that spend into the parent (else max_cost_usd is blind to runaway sub-agents)."""
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            sub.total_input_tokens = 1234   # simulate spend before the timeout
            sub.total_output_tokens = 567
            await asyncio.sleep(30)
            return {"text": "never", "tokens": {"input": 0, "output": 0}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    before_in, before_out = parent.total_input_tokens, parent.total_output_tokens
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p", "timeout_ms": 30}))
    assert "timed out" in res.lower()
    assert parent.total_input_tokens == before_in + 1234   # folded despite timeout
    assert parent.total_output_tokens == before_out + 567


def test_timed_out_subagent_envelope_surfaces_files_modified():
    """A sub-agent that modified files then timed out must give the parent a
    breadcrumb (files_modified + a pointer), not a bare '[timed out]' string."""
    parent = _agent()
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt):
            sub._files_modified.add("/repo/touched.py")   # host-observed before timeout
            await asyncio.sleep(30)

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    res = asyncio.run(parent._execute_agent_tool(
        {"type": "coder", "description": "d", "prompt": "p", "timeout_ms": 30}))
    assert "timed out" in res.lower()
    assert "touched.py" in res          # files_modified surfaced
    # subagent record points at a persisted result.md
    sub = parent.task_manager.get_subagent("agent-001")
    assert sub.last_result_path and sub.last_result_path.endswith("result.md")
