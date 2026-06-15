"""docs/18 Phase 7：Pi Branch Summarization 强化。

- 切 A→B：B 只看到 branch summary，不看 A 的 raw messages；
- 无 abandoned 内容 → 不生成 summary（纯 move_to）；
- custom focus 进 prompt + details；
- common-ancestor / file tracking / 限长 tool result；nested branch_summary details 累计；
- branch_summary entry 挂 target、成新 leaf。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.session import branch_summary as bs
from nanocode.session import tree
from nanocode.session.manager import SessionManager


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def _assistant(text, *, tool_calls=None, stop="stop"):
    content = [tree.text_block(text)] if text else []
    for tc in (tool_calls or []):
        content.append(tree.tool_call_block(tc["id"], tc["name"], tc.get("args", {})))
    return tree.assistant_message(content, provider="anthropic", api="anthropic",
                                  model="claude-x", stop_reason=stop)


# ── 切换后只见 summary、不见 raw messages ──────────────────────────────────────
def test_branch_b_sees_only_summary_not_branch_a_raw(monkeypatch):
    a = _agent("bs_basic")
    mgr = SessionManager.create("bs_basic")
    a._session_mgr = mgr
    root_q = mgr.append_message(tree.user_message("shared question"))
    mgr.append_message(_assistant("shared answer"))
    # branch A 内容（将被离开）
    mgr.append_message(tree.user_message("SECRET_BRANCH_A_QUESTION"))
    old_leaf = mgr.append_message(_assistant("SECRET_BRANCH_A_ANSWER"))

    async def fake_summary(messages, prompt):
        assert "abandoned branch" in prompt.lower()
        return "BRANCH-A-SUMMARY"

    monkeypatch.setattr(a, "_summarize_anthropic", fake_summary)
    msgs = asyncio.run(a.agent_session.move_to_with_branch_summary(root_q.id))
    blob = str(msgs)
    assert "BRANCH-A-SUMMARY" in blob
    assert "SECRET_BRANCH_A_QUESTION" not in blob       # 新 branch 不见 A 的 raw messages
    assert "SECRET_BRANCH_A_ANSWER" not in blob
    summaries = [e for e in mgr.entries() if e.type == tree.BRANCH_SUMMARY]
    assert len(summaries) == 1
    assert summaries[0].parentId == root_q.id           # 挂在 target 下
    assert mgr.get_leaf() == summaries[0].id            # 成为新 leaf
    assert summaries[0].data["fromId"] == old_leaf.id


# ── 无 abandoned 内容 → 不生成 summary ─────────────────────────────────────────
def test_no_abandoned_content_plain_move(monkeypatch):
    a = _agent("bs_empty")
    mgr = SessionManager.create("bs_empty")
    a._session_mgr = mgr
    u = mgr.append_message(tree.user_message("q"))
    leaf = mgr.append_message(_assistant("a"))
    called = {"n": 0}

    async def fake_summary(messages, prompt):
        called["n"] += 1
        return "X"

    monkeypatch.setattr(a, "_summarize_anthropic", fake_summary)
    # 切到当前 leaf（无 abandoned）→ 纯 move_to，不调用 summarizer
    asyncio.run(a.agent_session.move_to_with_branch_summary(leaf.id))
    assert called["n"] == 0
    assert [e for e in mgr.entries() if e.type == tree.BRANCH_SUMMARY] == []


# ── custom focus 进 prompt + details ───────────────────────────────────────────
def test_custom_focus_enters_prompt_and_details(monkeypatch):
    a = _agent("bs_focus")
    mgr = SessionManager.create("bs_focus")
    a._session_mgr = mgr
    target = mgr.append_message(tree.user_message("base"))
    mgr.append_message(tree.user_message("branch q"))
    mgr.append_message(_assistant("branch a"))
    seen = {}

    async def fake_summary(messages, prompt):
        seen["prompt"] = prompt
        return "S"

    monkeypatch.setattr(a, "_summarize_anthropic", fake_summary)
    asyncio.run(a.agent_session.move_to_with_branch_summary(target.id, focus="keep the migration plan"))
    assert "keep the migration plan" in seen["prompt"]
    assert "## Additional Instructions" in seen["prompt"]
    summ = [e for e in mgr.entries() if e.type == tree.BRANCH_SUMMARY][0]
    assert summ.data["details"]["focus"] == "keep the migration plan"
    assert summ.data["details"]["targetId"] == target.id
    assert summ.data["details"]["sourceLeafId"] is not None
    assert summ.data["details"]["commonAncestorId"] == target.id


# ── file tracking：真实 tool call 参数累计；nested branch_summary details 累计 ──
def test_file_tracking_from_tool_calls_and_nested_details(monkeypatch):
    a = _agent("bs_files")
    mgr = SessionManager.create("bs_files")
    a._session_mgr = mgr
    target = mgr.append_message(tree.user_message("base"))
    # abandoned branch：read_file + edit_file 的 toolCall
    mgr.append_message(tree.user_message("do work"))
    mgr.append_message(_assistant("", tool_calls=[
        {"id": "t1", "name": "read_file", "args": {"file_path": "/repo/read_me.py"}},
        {"id": "t2", "name": "edit_file", "args": {"file_path": "/repo/edit_me.py"}},
    ], stop="toolUse"))
    mgr.append_message(tree.tool_result_message(tool_call_id="t1", tool_name="read_file", content="x"))
    mgr.append_message(tree.tool_result_message(tool_call_id="t2", tool_name="edit_file", content="y"))
    # 一个既有的 branch_summary entry（nested），其 details 应被累计
    mgr.append_branch_summary(summary="prior", from_id=None,
                              details={"readFiles": ["/repo/prior_read.py"], "modifiedFiles": []})

    async def fake_summary(messages, prompt):
        return "S"

    monkeypatch.setattr(a, "_summarize_anthropic", fake_summary)
    asyncio.run(a.agent_session.move_to_with_branch_summary(target.id))
    d = [e for e in mgr.entries() if e.type == tree.BRANCH_SUMMARY
         and e.data["summary"] == "S"][0].data["details"]
    assert d["readFiles"] == ["/repo/prior_read.py", "/repo/read_me.py"]
    assert d["modifiedFiles"] == ["/repo/edit_me.py"]


# ── review HIGH：切到 root（target_id=None）时不得泄漏被离开 branch 的 raw messages ──
def test_branch_summary_to_root_does_not_leak_abandoned(monkeypatch):
    a = _agent("bs_root")
    mgr = SessionManager.create("bs_root")
    a._session_mgr = mgr
    # 第一条 user 的 parentId 为 None；/tree checkout 该 user → target=None（切到 root 前）
    mgr.append_message(tree.user_message("SECRET_A_Q"))
    old_leaf = mgr.append_message(_assistant("SECRET_A_ANSWER"))

    async def fake_summary(messages, prompt):
        return "ROOT-BRANCH-SUMMARY"

    monkeypatch.setattr(a, "_summarize_anthropic", fake_summary)
    assert a.agent_session.branch_summary_available(None) is True
    msgs = asyncio.run(a.agent_session.move_to_with_branch_summary(None))
    blob = str(msgs)
    assert "ROOT-BRANCH-SUMMARY" in blob
    assert "SECRET_A_Q" not in blob and "SECRET_A_ANSWER" not in blob   # 不泄漏
    summaries = [e for e in mgr.entries() if e.type == tree.BRANCH_SUMMARY]
    assert len(summaries) == 1
    # branch_summary 挂到 root（parentId=None），而非旧 leaf
    assert summaries[0].parentId is None
    assert summaries[0].data["fromId"] == old_leaf.id
    # 新 leaf 的 branch 只含 summary 一条（无 abandoned raw messages）
    branch_ids = [e.id for e in mgr.get_branch()]
    assert branch_ids == [summaries[0].id]


# ── 模块纯函数单元 ─────────────────────────────────────────────────────────────
def test_collect_common_ancestor_and_abandoned():
    mgr = SessionManager.create("bs_collect")
    root = mgr.append_message(tree.user_message("r"))
    a1 = mgr.append_message(_assistant("a1"))
    u2 = mgr.append_message(tree.user_message("u2"))
    leaf = mgr.append_message(_assistant("a2"))
    abandoned, ca = bs.collect_entries_for_branch_summary(mgr, leaf.id, a1.id)
    assert ca == a1.id
    assert [e.id for e in abandoned] == [u2.id, leaf.id]


def test_serialize_caps_tool_result():
    e = tree.Entry(type=tree.MESSAGE, id="tr", parentId=None, sessionId="s", timestamp="t",
                   data={"message": tree.tool_result_message(
                       tool_call_id="x", tool_name="run_shell", content="Z" * 5000)})
    out = bs.serialize_branch_conversation([e])
    assert "truncated" in out
    assert len(out) < 5000


def test_prepare_branch_entries_newest_first_budget():
    mgr = SessionManager.create("bs_budget")
    mgr.append_message(tree.user_message("oldest " * 100))
    mgr.append_message(tree.user_message("middle " * 100))
    newest = mgr.append_message(tree.user_message("newest"))
    entries = mgr.get_branch()[1:]   # 去掉 session_start
    chosen = bs.prepare_branch_entries(entries, token_budget=5)
    # 预算极小 → 只保留最新一条（最旧/中间被丢，至少保最新）
    assert {e.id for e in chosen} == {newest.id}
