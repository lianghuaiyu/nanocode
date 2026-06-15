"""Phase 0（docs/18）：锁定 Pi-style 树的 compaction / branch-summary / 注入不变量。

这些不变量是后续 Phase 1-7 的安全网——任何对 cut point / summary prompt / details /
post-compact restore / branch summary 的改动都不得破坏它们。纯函数为主（fold/convert_to_llm/
render），少量走 AgentSession 的请求装配以锁 repo-map-不落树边界。
"""

from __future__ import annotations

import types

from nanocode.session import context, tree
from nanocode.session.render import ModelCtx, render
from nanocode.session.tree import Entry


def _e(eid, parent, etype, **data):
    return Entry(type=etype, id=eid, parentId=parent, sessionId="s",
                 timestamp="2026-01-01T00:00:00Z", data=data)


def _contents(neutral):
    return " | ".join(str(m.get("content")) for m in neutral)


# ── (a) 两区 fold：只保留 summary + firstKeptEntryId 起的 suffix ────────────────
def test_compaction_fold_keeps_only_summary_and_kept_suffix():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("u1", "r", tree.MESSAGE, message=tree.user_message("PRE_DROP_HISTORY")),
        _e("a1", "u1", tree.MESSAGE, message=tree.assistant_message(
            [tree.text_block("PRE_DROP_ANSWER")], provider="anthropic", api="anthropic",
            model="claude-x", stop_reason="stop")),
        _e("u2", "a1", tree.MESSAGE, message=tree.user_message("KEPT_QUESTION")),
        _e("c", "u2", tree.COMPACTION, summary="THE_SUMMARY", firstKeptEntryId="u2",
           tokensBefore=999),
        _e("u3", "c", tree.MESSAGE, message=tree.user_message("AFTER_COMPACTION")),
    ]
    rich, _ = context.fold(branch)
    neutral = context.convert_to_llm(rich)
    blob = _contents(neutral)
    # summary + kept-suffix（firstKept 起）保留
    assert "THE_SUMMARY" in blob
    assert "KEPT_QUESTION" in blob
    assert "AFTER_COMPACTION" in blob
    # firstKept 之前的前区被 summary 顶替
    assert "PRE_DROP_HISTORY" not in blob
    assert "PRE_DROP_ANSWER" not in blob
    # summary 落成 user 消息且带 COMPACTION_PREFIX
    summ = [m for m in neutral if isinstance(m.get("content"), str)
            and "THE_SUMMARY" in m["content"]]
    assert len(summ) == 1
    assert summ[0]["role"] == "user"
    assert summ[0]["content"].startswith(context.COMPACTION_PREFIX)
    assert summ[0]["content"].endswith(context.COMPACTION_SUFFIX)


# ── (b) firstKeptEntryId=None：无 kept suffix（前区全顶替，不双计旧历史）──────────
def test_compaction_first_kept_none_drops_whole_pre_region():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("u1", "r", tree.MESSAGE, message=tree.user_message("ANCIENT_HISTORY")),
        _e("a1", "u1", tree.MESSAGE, message=tree.assistant_message(
            [tree.text_block("ANCIENT_RESPONSE")], provider="anthropic", api="anthropic",
            model="claude-x", stop_reason="stop")),
        _e("c", "a1", tree.COMPACTION, summary="ALL_REPLACED", firstKeptEntryId=None),
    ]
    rich, _ = context.fold(branch)
    neutral = context.convert_to_llm(rich)
    blob = _contents(neutral)
    assert "ALL_REPLACED" in blob
    # 无 kept suffix：前区一条不留（绝不 "summary + 原文" 双份）
    assert "ANCIENT_HISTORY" not in blob
    assert "ANCIENT_RESPONSE" not in blob
    # 只剩 summary 一条 user 消息
    users = [m for m in neutral if m.get("role") == "user"]
    assert len(users) == 1


# ── (c) branch_summary → user 消息，带 PREFIX/SUFFIX；空 summary 不注入 ──────────
def test_branch_summary_converts_to_wrapped_user_message():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("bs", "r", tree.BRANCH_SUMMARY, summary="EXPLORED_BRANCH_X", fromId="r"),
    ]
    rich, _ = context.fold(branch)
    neutral = context.convert_to_llm(rich)
    bs = [m for m in neutral if isinstance(m.get("content"), str)
          and "EXPLORED_BRANCH_X" in m["content"]]
    assert len(bs) == 1
    assert bs[0]["role"] == "user"
    assert bs[0]["content"].startswith(context.BRANCH_SUMMARY_PREFIX)
    assert bs[0]["content"].endswith(context.BRANCH_SUMMARY_SUFFIX)
    # branch_summary 用 PREFIX/SUFFIX，绝不用 compaction 的 prefix
    assert not bs[0]["content"].startswith(context.COMPACTION_PREFIX)


def test_empty_branch_summary_not_injected():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("bs", "r", tree.BRANCH_SUMMARY, summary="   ", fromId="r"),
    ]
    rich, _ = context.fold(branch)
    assert [m for m in rich if m.get("role") == "branchSummary"] == []


# ── (d) custom_message 原样注入（无 PREFIX，否则改写注入的 <system-reminder>）───
def test_custom_message_injected_verbatim_no_prefix():
    sent = "<system-reminder>project instructions verbatim</system-reminder>"
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("cm", "r", tree.CUSTOM_MESSAGE, customType="project_instructions",
           content=sent, display=False),
    ]
    rich, _ = context.fold(branch)
    neutral = context.convert_to_llm(rich)
    cm = [m for m in neutral if m.get("content") == sent]
    assert len(cm) == 1
    assert cm[0]["role"] == "user"
    # 原样：既不加 compaction 也不加 branch-summary 前缀
    assert not cm[0]["content"].startswith(context.COMPACTION_PREFIX)
    assert not cm[0]["content"].startswith(context.BRANCH_SUMMARY_PREFIX)


# ── (e) repo map 永不落 session.jsonl（volatile tail = 请求局部，persist=none）───
def test_repo_map_volatile_pack_never_persists_to_tree():
    from nanocode.agent.engine import Agent
    from nanocode.context.cache_policy import survives_compaction
    from nanocode.context.packs import ContextPack
    from nanocode.session.manager import SessionManager

    a = Agent(api_key="test", session_id="ci_rmap", permission_mode="bypassPermissions")
    a.model = "claude-x"
    mgr = SessionManager.create("ci_rmap")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("real user turn"))

    pack = ContextPack(id="repo_map", kind="repo_map",
                       content="# Repo map\n- applib.py: REPO_MAP_MARKER_FN",
                       lifecycle="turn", cache_policy="volatile_tail", persist_policy="none",
                       priority=30)
    # 结构不变量：repo map 是 turn-volatile，compaction 后绝不存活
    assert survives_compaction(pack) is False

    a._turn_context_plan = types.SimpleNamespace(packs=[pack])
    tail = a.agent_session._volatile_tail()
    req = a.agent_session.build_request_messages(extra_neutral=tail)

    # 请求尾部携带 repo map（volatile tail）
    assert "REPO_MAP_MARKER_FN" in str(req)
    # 树里绝无 repo map 文本（persist=none → 从不 append 成 entry）
    tree_blob = str([e.to_dict() for e in mgr.entries()])
    assert "REPO_MAP_MARKER_FN" not in tree_blob
    assert "# Repo map" not in tree_blob


# ── (f) render 后无 inverse-orphan toolResult（被 abort 的回合留下的孤儿被清理）──
def test_render_drops_inverse_orphan_tool_result():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("u", "r", tree.MESSAGE, message=tree.user_message("do something")),
        # 被 abort 的 assistant（含 toolCall t1）——render 的 thinking/stop gate 会丢弃它
        _e("a", "u", tree.MESSAGE, message=tree.assistant_message(
            [tree.tool_call_block("t1", "run_shell", {"command": "ls"})],
            provider="anthropic", api="anthropic", model="claude-x", stop_reason="aborted")),
        # 其 toolResult 成为 inverse-orphan（toolCall 已随 aborted assistant 被丢）
        _e("tr", "a", tree.MESSAGE, message=tree.tool_result_message(
            tool_call_id="t1", tool_name="run_shell", content="ORPHAN_RESULT_PAYLOAD")),
    ]
    rich, _ = context.fold(branch)
    neutral = context.convert_to_llm(rich)
    rendered = render(neutral, ModelCtx(provider="anthropic", api="anthropic",
                                        model_id="claude-x"))
    blob = str(rendered["messages"])
    # 孤儿 toolResult 被清掉（render normalize 的 inverse-orphan 清理）
    assert "ORPHAN_RESULT_PAYLOAD" not in blob


def test_render_drops_tool_result_with_no_matching_call():
    # 直接的 inverse-orphan：toolResult 的 toolCallId 在 branch 内无对应 toolCall
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("u", "r", tree.MESSAGE, message=tree.user_message("hi")),
        _e("tr", "u", tree.MESSAGE, message=tree.tool_result_message(
            tool_call_id="ghost", tool_name="run", content="DANGLING_RESULT")),
    ]
    rich, _ = context.fold(branch)
    neutral = context.convert_to_llm(rich)
    rendered = render(neutral, ModelCtx(provider="anthropic", api="anthropic",
                                        model_id="claude-x"))
    assert "DANGLING_RESULT" not in str(rendered["messages"])
