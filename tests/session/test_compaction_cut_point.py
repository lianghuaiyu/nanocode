"""docs/18 Phase 2：safe cut point 与 split-turn 支持。

- 优先 user 边界；超长单 turn 允许在 assistant/custom/branch_summary/compaction 切（split-turn）；
- 永不 cut at toolResult；候选 cut 经 fold→convert_to_llm→render + 无 inverse-orphan 验证；
- 仅 user 自身超预算时兜底保留当前问题（不摘要掉）。
"""

from nanocode.agent.engine import Agent
from nanocode.session import tree
from nanocode.session.manager import SessionManager
from nanocode.session.render import ModelCtx, render


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def _cut(a, budget):
    a.agent_session.keep_recent_tokens = lambda: budget
    return a.agent_session._compaction_cut(a._session_mgr.get_branch())


def _renders_legal(branch, cut_id, *, openai=False):
    idx = next(i for i, e in enumerate(branch) if e.id == cut_id)
    from nanocode.session import context as ctx
    rich, _ = ctx.fold(branch[idx:])
    neutral = ctx.convert_to_llm(rich)
    api = "openai-completions" if openai else "anthropic"
    provider = "openai" if openai else "anthropic"
    out = render(neutral, ModelCtx(provider=provider, api=api, model_id="claude-x"))
    return out["messages"]


# ── 普通：预算内最近 user 边界 ─────────────────────────────────────────────────
def test_cut_at_recent_user_boundary_within_budget():
    a = _agent("cut_user")
    mgr = SessionManager.create("cut_user")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("old " * 50))
    mgr.append_message(tree.assistant_message([tree.text_block("a1 " * 30)], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    recent = mgr.append_message(tree.user_message("recent"))
    cp = _cut(a, 5)
    assert cp.first_kept_id == recent.id
    assert cp.is_split_turn is False
    assert cp.cut_entry_type == "message"


# ── 超长单 turn：在 assistant 边界 split-turn ──────────────────────────────────
def test_split_turn_cuts_inside_oversized_turn_at_assistant():
    a = _agent("cut_split")
    mgr = SessionManager.create("cut_split")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("go"))                       # 小 user
    mgr.append_message(tree.assistant_message(
        [tree.tool_call_block("t1", "run_shell", {"command": "x"})],
        provider="anthropic", api="anthropic", model="claude-x", stop_reason="toolUse"))
    mgr.append_message(tree.tool_result_message(
        tool_call_id="t1", tool_name="run_shell", content="HUGE " * 5000))   # 巨大 tool result
    a2 = mgr.append_message(tree.assistant_message([tree.text_block("done")], provider="anthropic",
                            api="anthropic", model="claude-x", stop_reason="stop"))
    branch = mgr.get_branch()
    cp = _cut(a, 100)
    # split-turn：cut 在最后的 assistant，巨大 tool result 进 summary（真实收缩）
    assert cp.first_kept_id == a2.id
    assert cp.is_split_turn is True
    assert cp.cut_entry_type == "message"
    # kept suffix 不含巨大 tool result
    idx = next(i for i, e in enumerate(branch) if e.id == a2.id)
    kept_blob = str([e.to_dict() for e in branch[idx:]])
    assert "HUGE HUGE" not in kept_blob
    # 两 provider 都渲染合法
    assert _renders_legal(branch, a2.id, openai=False)
    assert _renders_legal(branch, a2.id, openai=True)


# ── 永不 cut at toolResult（即使它是 leaf）──────────────────────────────────────
def test_never_cuts_at_tool_result():
    a = _agent("cut_notr")
    mgr = SessionManager.create("cut_notr")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("q " * 80))
    mgr.append_message(tree.assistant_message(
        [tree.tool_call_block("t1", "run", {})], provider="anthropic", api="anthropic",
        model="claude-x", stop_reason="toolUse"))
    tr = mgr.append_message(tree.tool_result_message(tool_call_id="t1", tool_name="run",
                            content="result " * 50))
    branch = mgr.get_branch()
    cp = _cut(a, 20)
    assert cp.first_kept_id != tr.id            # 绝不以 toolResult 作 kept-suffix 头
    # cut 头必是合法 entry（user/assistant/custom/compaction/branch_summary）
    head = next(e for e in branch if e.id == cp.first_kept_id)
    assert a.agent_session._is_valid_cut_entry(head)


# ── 仅 user 自身超预算 → 兜底保留当前问题（不 split 掉问题）────────────────────
def test_oversized_user_message_kept_not_split_away():
    a = _agent("cut_bigq")
    mgr = SessionManager.create("cut_bigq")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("old history"))
    mgr.append_message(tree.assistant_message([tree.text_block("a1")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    big_user = mgr.append_message(tree.user_message("recent question " * 100))   # 巨大 user
    mgr.append_message(tree.assistant_message([tree.text_block("ans")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    cp = _cut(a, 1)
    assert cp.first_kept_id == big_user.id      # 当前问题原文保留，不被摘要掉
    assert cp.is_split_turn is False


# ── helper 单元 ────────────────────────────────────────────────────────────────
def test_entry_token_estimate_and_valid_cut_entry():
    a = _agent("cut_helpers")
    s = a.agent_session
    u = tree.Entry(type=tree.MESSAGE, id="u", parentId=None, sessionId="s", timestamp="t",
                   data={"message": tree.user_message("hello world")})
    tr = tree.Entry(type=tree.MESSAGE, id="tr", parentId="u", sessionId="s", timestamp="t",
                    data={"message": tree.tool_result_message(tool_call_id="x", tool_name="run",
                                                              content="r")})
    cm = tree.Entry(type=tree.CUSTOM_MESSAGE, id="cm", parentId="u", sessionId="s", timestamp="t",
                    data={"customType": "skill", "content": "body"})
    assert s._is_valid_cut_entry(u) is True
    assert s._is_valid_cut_entry(tr) is False           # toolResult 非法
    assert s._is_valid_cut_entry(cm) is True
    assert s._entry_token_estimate(u) >= 1
    telemetry = tree.Entry(type=tree.TURN_END, id="te", parentId="u", sessionId="s", timestamp="t",
                           data={})
    assert s._entry_token_estimate(telemetry) == 0
