"""S4 compaction-as-entry：summary-compaction additive 写 compaction 树 entry，build_context 两区 fold。"""

from nanocode.agent.engine import Agent
from nanocode.session import tree
from nanocode.session.manager import SessionManager


async def _fake_summary():
    return "THE SUMMARY"


def test_append_compaction_and_fold_two_regions():
    m = SessionManager.create("s4a")
    m.append_message(tree.user_message("old conversation"))
    kept = m.append_message(tree.user_message("kept user msg"))   # 压缩时保留的 last-user
    m.append_compaction(summary="SUMMARY OF OLD", tokens_before=12345,
                        first_kept_entry_id=kept.id)
    m.append_message(tree.assistant_message([tree.text_block("after")], provider="anthropic",
                     api="anthropic", model="claude-x", stop_reason="stop"))
    msgs = m.build_context().messages
    # fold：摘要(合成 user) + firstKept 起的消息（kept user + after assistant）；"old conversation" 被顶替
    joined = " | ".join(str(x.get("content")) for x in msgs)
    assert "SUMMARY OF OLD" in joined
    assert "kept user msg" in joined
    assert "old conversation" not in joined
    assert msgs[-1]["role"] == "assistant"


def test_compaction_entry_persisted_and_reopen():
    m = SessionManager.create("s4b")
    u = m.append_message(tree.user_message("u"))
    m.append_compaction(summary="S", first_kept_entry_id=u.id)
    reopened = SessionManager.open("s4b")
    comp = [e for e in reopened.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1 and comp[0].data["summary"] == "S"
    assert comp[0].data["firstKeptEntryId"] == u.id


def test_compacted_session_resumes_summary_from_tree():
    # 压缩后的树（old + kept + compaction + after）resume → 摘要保留、被压缩史不复现。
    m = SessionManager.create("cr1")
    m.append_message(tree.user_message("ancient history blah"))
    kept = m.append_message(tree.user_message("kept question"))
    m.append_compaction(summary="SUMMARY-OF-OLD", first_kept_entry_id=kept.id)
    m.append_message(tree.assistant_message([tree.text_block("answer")], provider="anthropic",
                     api="anthropic", model="claude-x", stop_reason="stop"))
    b = Agent(api_key="test", trace_enabled=False, session_id="cr1", permission_mode="bypassPermissions")
    b.model = "claude-x"
    snap = [{"role": "user", "content": "[Previous conversation summary]\nSUMMARY-OF-OLD"},
            {"role": "user", "content": "kept question"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}]
    b.restore_session({"anthropicMessages": snap})
    joined = str(b._anthropic_messages)
    assert "SUMMARY-OF-OLD" in joined          # 摘要经树 resume 保留
    assert "ancient history" not in joined     # 被压缩史不复现


def test_last_user_message_id_picks_last_user_not_leaf():
    # bug#1：compaction cut point = 最后一条 user 消息，而非 live leaf（其后可能是 assistant/tool）。
    m = SessionManager.create("s4cut")
    m.append_message(tree.user_message("u1"))
    last_user = m.append_message(tree.user_message("u2"))
    m.append_message(tree.assistant_message([tree.text_block("a")], provider="anthropic",
                     api="anthropic", model="claude-x", stop_reason="toolUse"))
    tr = m.append_message(tree.tool_result_message(tool_call_id="t", tool_name="run", content="r"))
    assert m.get_leaf() == tr.id and m.get_leaf() != last_user.id    # live leaf 是 tool_result
    assert m.last_user_message_id() == last_user.id                  # cut point 指向 last-user


def test_engine_compaction_auto_cut_point_is_last_user_when_leaf(monkeypatch):
    # auto-compact 触发于刚记完 user 消息（leaf == 该 user）→ firstKeptEntryId = 该 user（保留它）。
    import asyncio
    a = Agent(api_key="test", trace_enabled=False, session_id="s4auto", permission_mode="bypassPermissions")
    a.model = "claude-x"
    mgr = SessionManager.create("s4auto")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("old"))
    mgr.append_message(tree.assistant_message([tree.text_block("a1")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    last_user = mgr.append_message(tree.user_message("recent question"))   # leaf == this user
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    asyncio.run(a._compact_conversation())
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1 and comp[0].data["firstKeptEntryId"] == last_user.id


def test_engine_compaction_manual_cut_point_none_when_leaf_is_assistant(monkeypatch):
    # manual /compact 在 turn 间（leaf = assistant）→ firstKeptEntryId = None（旧消息全被 summary 顶替，
    # 与 backend 行为一致：末条非 user 时 summary 后不留旧消息，docs/14 P3 review #5）。
    import asyncio
    a = Agent(api_key="test", trace_enabled=False, session_id="s4man", permission_mode="bypassPermissions")
    a.model = "claude-x"
    mgr = SessionManager.create("s4man")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("recent question"))
    mgr.append_message(tree.assistant_message([tree.text_block("ans")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))   # leaf = assistant
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    asyncio.run(a._compact_conversation())
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1 and comp[0].data["firstKeptEntryId"] is None
