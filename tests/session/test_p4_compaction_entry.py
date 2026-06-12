"""S4 compaction-as-entry：summary-compaction additive 写 compaction 树 entry，build_context 两区 fold。"""

from nanocode.agent.engine import Agent
from nanocode.session import tree
from nanocode.session.manager import SessionManager


async def _fake_summary(_messages=None):
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
    # docs/14 SessionLease：resume = lease + 按轮树重渲染（docs/16 #3c：flat 装载已退役）。
    from nanocode.session.lease import SessionLease
    m = SessionManager.create("cr1")
    m.append_message(tree.user_message("ancient history blah"))
    kept = m.append_message(tree.user_message("kept question"))
    m.append_compaction(summary="SUMMARY-OF-OLD", first_kept_entry_id=kept.id)
    m.append_message(tree.assistant_message([tree.text_block("answer")], provider="anthropic",
                     api="anthropic", model="claude-x", stop_reason="stop"))
    m.close()
    b = Agent(api_key="test", session_id="cr1", permission_mode="bypassPermissions")
    b.model = "claude-x"
    b._session_mgr = SessionLease.open_or_create("cr1").manager
    joined = str(b.agent_session.build_request_messages())
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


def test_compaction_cut_point_keeps_recent_user_within_budget(monkeypatch):
    # docs/16 #10 keepRecentTokens：firstKept = 预算内最近的 user MESSAGE 边界；
    # summarizer 只吃 prefix（kept suffix 绝不进 summary——两区 fold 不双计）。
    import asyncio
    a = Agent(api_key="test", session_id="s4auto", permission_mode="bypassPermissions")
    a.model = "claude-x"
    mgr = SessionManager.create("s4auto")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("old question " * 50))
    mgr.append_message(tree.assistant_message([tree.text_block("a1 " * 50)], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    last_user = mgr.append_message(tree.user_message("recent"))
    seen = {}

    async def _fake(messages=None):
        seen["messages"] = messages
        return "S"

    monkeypatch.setattr(a, "_compact_anthropic", _fake)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)   # 预算只容末条 user
    asyncio.run(a.agent_session.compact())
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1 and comp[0].data["firstKeptEntryId"] == last_user.id
    assert "old question" in str(seen["messages"])      # prefix 进 summarizer
    assert "recent" not in str(seen["messages"])        # kept suffix 不进 summarizer


def test_compaction_cut_point_none_when_no_user_boundary_fits(monkeypatch):
    # 末条 user 自身已超预算 → 无可保的 user 边界 → firstKept=None（旧消息全由 summary 顶替）。
    import asyncio
    a = Agent(api_key="test", session_id="s4man", permission_mode="bypassPermissions")
    a.model = "claude-x"
    mgr = SessionManager.create("s4man")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("recent question " * 100))
    mgr.append_message(tree.assistant_message([tree.text_block("ans")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    monkeypatch.setattr(a, "_compact_anthropic", _fake_summary)
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 1)
    asyncio.run(a.agent_session.compact())
    comp = [e for e in mgr.entries() if e.type == tree.COMPACTION]
    assert len(comp) == 1 and comp[0].data["firstKeptEntryId"] is None


def test_compaction_skipped_when_everything_fits_keep_budget(monkeypatch):
    # 整个对话都在 keep 预算内 → prefix 为空 → summarizer 拿 None → 不写 compaction entry
    # （无可压缩内容时绝不写空 summary 顶替历史）。
    import asyncio
    a = Agent(api_key="test", session_id="s4fit", permission_mode="bypassPermissions")
    a.model = "claude-x"
    mgr = SessionManager.create("s4fit")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("q"))
    mgr.append_message(tree.assistant_message([tree.text_block("a")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))

    async def _fake(messages=None):
        return None if not messages else "S"

    monkeypatch.setattr(a, "_compact_anthropic", _fake)
    asyncio.run(a.agent_session.compact())          # 默认预算远大于内容
    assert [e for e in mgr.entries() if e.type == tree.COMPACTION] == []
