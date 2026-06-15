"""docs/18 Phase 4：compaction entry details + 累计 file tracking。"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.session import tree
from nanocode.session.manager import SessionManager


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def _seed(mgr):
    mgr.append_message(tree.user_message("old question " * 50))
    mgr.append_message(tree.assistant_message([tree.text_block("a1 " * 20)], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(tree.user_message("recent"))


async def _fake_summary(messages=None, instructions=None):
    return "SUMMARY"


def test_compaction_entry_carries_details(monkeypatch):
    a = _agent("d_basic")
    mgr = SessionManager.create("d_basic")
    a._session_mgr = mgr
    _seed(mgr)
    a._files_read.add("/repo/foo.py")
    a._files_modified.add("/repo/bar.py")
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact())

    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1
    d = comp[0].data["details"]
    assert d is not None
    assert d["trigger"] == "manual"                 # 直接 compact() 调用 = manual
    assert d["reason"] == "context_window"
    assert d["cutEntryType"] == "message"
    assert d["isSplitTurn"] is False
    assert d["retryCount"] == 0
    assert d["readFiles"] == ["/repo/foo.py"]
    assert d["modifiedFiles"] == ["/repo/bar.py"]
    assert d["messageCountBefore"] >= 1
    assert d["messageCountAfter"] >= 1
    assert d["estimatedPostCompactTokens"] >= 1
    # 既有 top-level 字段保持（trajectory / tree_view 消费），未被 details 取代
    assert comp[0].data["summary"] == "SUMMARY"
    assert comp[0].data["firstKeptEntryId"] is not None


def test_auto_trigger_recorded_in_details(monkeypatch):
    a = _agent("d_auto")
    mgr = SessionManager.create("d_auto")
    a._session_mgr = mgr
    _seed(mgr)
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    a.last_input_token_count = a.effective_window
    asyncio.run(a.agent_session.check_and_compact())
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert comp[0].data["details"]["trigger"] == "auto"
    assert comp[0].data["kind"] == "auto"


def test_manual_compact_with_instructions_marks_manual(monkeypatch):
    a = _agent("d_manual")
    mgr = SessionManager.create("d_manual")
    a._session_mgr = mgr
    _seed(mgr)
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact("focus on the API"))
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    d = comp[0].data["details"]
    assert d["trigger"] == "manual" and d["reason"] == "manual"
    assert comp[0].data["kind"] == "manual"


def test_file_tracking_accumulates_across_compactions(monkeypatch):
    a = _agent("d_accum")
    mgr = SessionManager.create("d_accum")
    a._session_mgr = mgr
    _seed(mgr)
    a._files_read.add("/repo/a.py")
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact())

    # 模拟 resume/fork：rebind 清空 live 集合，但树里的上一代 compaction.details 仍累计
    a._files_read = {"/repo/b.py"}
    a._files_modified = set()
    mgr.append_message(tree.user_message("more turns " * 50))
    mgr.append_message(tree.assistant_message([tree.text_block("x")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    mgr.append_message(tree.user_message("recent2"))
    asyncio.run(a.agent_session.compact())

    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 2
    latest = comp[-1].data["details"]
    assert set(latest["readFiles"]) == {"/repo/a.py", "/repo/b.py"}   # 跨代累计


def test_repo_map_files_never_enter_details(monkeypatch):
    # details 的 readFiles/modifiedFiles 只来自宿主真实工具观测，绝不从 repo map / 提及推断。
    a = _agent("d_nomap")
    mgr = SessionManager.create("d_nomap")
    a._session_mgr = mgr
    _seed(mgr)
    a._files_read.add("/repo/really_read.py")     # 真实读取（_on_file_touched 等价）
    # 注意：没有任何把 repo map 文件塞进 _files_read 的路径——tracking 只读 _files_read/_files_modified
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact())
    d = [e for e in mgr.entries() if e.type == tree.COMPACTION][0].data["details"]
    assert d["readFiles"] == ["/repo/really_read.py"]   # 只含真实读取，无 repo-map 泄漏


def test_file_tracking_recovers_from_branch_tool_calls(monkeypatch):
    # review：resume/rebind 后 live _files_* 被清空——压缩前已读的文件须从树里 assistant toolCall 找回。
    a = _agent("d_recover")
    mgr = SessionManager.create("d_recover")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("work " * 50))
    mgr.append_message(tree.assistant_message([
        tree.tool_call_block("t1", "read_file", {"file_path": "/repo/recovered.py"}),
        tree.tool_call_block("t2", "edit_file", {"file_path": "/repo/edited.py"}),
    ], provider="anthropic", api="anthropic", model="claude-x", stop_reason="toolUse"))
    mgr.append_message(tree.tool_result_message(tool_call_id="t1", tool_name="read_file", content="x"))
    mgr.append_message(tree.tool_result_message(tool_call_id="t2", tool_name="edit_file", content="y"))
    mgr.append_message(tree.user_message("recent"))
    a._files_read = set()       # 模拟 rebind 清空 live 集合
    a._files_modified = set()
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session.compact())
    d = [e for e in mgr.entries() if e.type == tree.COMPACTION][0].data["details"]
    assert d["readFiles"] == ["/repo/recovered.py"]     # 从树 toolCall 找回
    assert d["modifiedFiles"] == ["/repo/edited.py"]


def test_overflow_trigger_recorded_in_details(monkeypatch):
    # core 的 per-turn overflow 恢复（cfg.compact = _compact_on_overflow）→ trigger=overflow_retry。
    a = _agent("d_overflow")
    mgr = SessionManager.create("d_overflow")
    a._session_mgr = mgr
    _seed(mgr)
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    asyncio.run(a.agent_session._compact_on_overflow())
    d = [e for e in mgr.entries() if e.type == tree.COMPACTION][0].data["details"]
    assert d["trigger"] == "overflow_retry"
    assert d["reason"] == "prompt_too_long"
    assert [e for e in mgr.entries() if e.type == tree.COMPACTION][0].data["kind"] == "auto"
