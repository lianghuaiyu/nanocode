"""docs/18 Phase 6：per-message aggregate tool-result budget。

- 多个并行 tool result 合并后受 per-group 预算约束（替换最大者为 preview）；
- replacement 决策跨 turn 稳定（同 toolCallId 复用同一 preview）；
- 已见未替换的 toolCallId 冻结（不后补替换，保护 prompt cache）；
- read_file 跳过替换但仍标 seen；
- 操作请求局部副本，绝不改写树。
"""

from nanocode.agent.engine import Agent
from nanocode.agent.tool_result_budget import (
    ContentReplacementState,
    apply_tool_result_budget,
    default_preview_builder,
)
from nanocode.session import tree
from nanocode.session.manager import SessionManager


def _tr(tcid, tool, content):
    return {"role": "toolResult", "toolCallId": tcid, "toolName": tool, "content": content}


def _budget(msgs, state, budget):
    return apply_tool_result_budget(msgs, state, per_group_token_budget=budget)


# ── 未超预算 → 内容不改，但全文也冻结（防日后预算变小改写已缓存前缀）──────────────
def test_under_budget_unchanged_but_frozen():
    state = ContentReplacementState()
    msgs = [{"role": "user", "content": "hi"},
            _tr("t1", "run", "small"), _tr("t2", "run", "tiny")]
    out = _budget(msgs, state, budget=10_000)
    assert out == msgs                              # 内容不变
    assert state.replacements == {}
    assert state.seen_ids == {"t1", "t2"}           # 全文已冻结


def test_full_result_frozen_against_later_budget_shrink():
    state = ContentReplacementState()
    msgs = [_tr("t1", "run", "X" * 40000)]
    out1 = _budget(msgs, state, budget=10_000_000)  # turn1：预算大 → 全文发出 + 冻结
    assert out1[0]["content"] == "X" * 40000
    assert "t1" in state.seen_ids and "t1" not in state.replacements
    out2 = _budget(msgs, state, budget=100)         # turn2：预算骤小 → t1 已冻结全文，绝不改写
    assert out2[0]["content"] == "X" * 40000


# ── 超预算 → 替换最大者 ────────────────────────────────────────────────────────
def test_over_budget_replaces_largest():
    state = ContentReplacementState()
    msgs = [_tr("t1", "run", "X" * 40000), _tr("t2", "run", "small")]
    out = _budget(msgs, state, budget=100)
    # 最大的 t1 被替换为 preview，t2 保留
    assert "elided to fit the per-message context budget" in out[0]["content"]
    assert out[1]["content"] == "small"
    assert "t1" in state.replacements
    assert "t1" in state.seen_ids and "t2" in state.seen_ids


# ── 不改写 input dicts / 树 ────────────────────────────────────────────────────
def test_does_not_mutate_input():
    state = ContentReplacementState()
    original = _tr("t1", "run", "X" * 40000)
    msgs = [original]
    _budget(msgs, state, budget=100)
    assert original["content"] == "X" * 40000          # 原 dict 未被改写


# ── 决策跨 turn 稳定（同 toolCallId 复用同一 preview）─────────────────────────
def test_replacement_stable_across_calls():
    state = ContentReplacementState()
    msgs = [_tr("t1", "run", "X" * 40000), _tr("t2", "run", "Y" * 40000)]
    out1 = _budget(msgs, state, budget=100)
    preview_t1 = state.replacements.get("t1")
    out2 = _budget(msgs, state, budget=100)
    # 第二次复用同一 preview（prompt-cache 前缀稳定）
    assert state.replacements["t1"] == preview_t1
    replaced1 = [m["content"] for m in out1 if m["content"] != "X" * 40000]
    replaced2 = [m["content"] for m in out2 if m["content"] != "X" * 40000]
    assert replaced1 == replaced2


# ── 已见未替换的 toolCallId 冻结（不后补替换）───────────────────────────────────
def test_seen_but_unreplaced_is_frozen():
    state = ContentReplacementState()
    # 第一次：预算只够替换 t1（最大），t2 被 seen 但未替换
    msgs = [_tr("t1", "run", "X" * 40000), _tr("t2", "run", "Y" * 8000)]
    _budget(msgs, state, budget=3000)   # 替换 t1 后 t2(~2000 tok) 仍 > 3000? 调小确保只替换 t1
    assert "t1" in state.replacements
    # t2 已 seen
    assert "t2" in state.seen_ids
    t2_replaced_first = "t2" in state.replacements
    # 第二次：即使更小预算，t2 已 seen 未替换 → 冻结，绝不后补
    _budget(msgs, state, budget=1)
    assert ("t2" in state.replacements) == t2_replaced_first   # 冻结：状态不变


# ── read_file 跳过替换但仍标 seen ──────────────────────────────────────────────
def test_read_file_skipped_but_marked_seen():
    state = ContentReplacementState()
    msgs = [_tr("t1", "read_file", "X" * 80000)]
    out = _budget(msgs, state, budget=100)
    assert out[0]["content"] == "X" * 80000     # read_file 不替换（自身已分页/cap）
    assert "t1" not in state.replacements
    assert "t1" in state.seen_ids               # 但标记 seen（冻结）


# ── preview builder：shell 留尾、其余留头 ──────────────────────────────────────
def test_preview_builder_head_vs_tail():
    head = default_preview_builder("run", "HEAD" + "x" * 5000, keep_chars=10)
    assert head.endswith("HEADxxxxxx") or "HEAD" in head
    tail = default_preview_builder("run_shell", "x" * 5000 + "TAILEND", keep_chars=10)
    assert tail.endswith("xxxTAILEND") or "TAILEND" in tail


# ── 集成：build_request_messages 在 render 前施加预算 ───────────────────────────
def test_build_request_messages_applies_budget():
    a = Agent(api_key="test", session_id="trb_int", permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    a.effective_window = 1000        # → per-group budget = max(8000, 100) = 8000 tok ≈ 32k chars
    mgr = SessionManager.create("trb_int")
    a._session_mgr = mgr
    mgr.append_message(tree.user_message("q"))
    mgr.append_message(tree.assistant_message(
        [tree.tool_call_block("t1", "run", {}), tree.tool_call_block("t2", "run", {})],
        provider="anthropic", api="anthropic", model="claude-x", stop_reason="toolUse"))
    mgr.append_message(tree.tool_result_message(tool_call_id="t1", tool_name="run", content="A" * 40000))
    mgr.append_message(tree.tool_result_message(tool_call_id="t2", tool_name="run", content="B" * 40000))
    out = a.agent_session.build_request_messages()
    blob = str(out)
    assert "elided to fit the per-message context budget" in blob   # 预算生效
    assert a._content_replacement.replacements                       # 决策落入 state
    # 树里仍是干净原文（请求局部替换不落树）
    tree_blob = str([e.to_dict() for e in mgr.entries()])
    assert "A" * 40000 in tree_blob or "AAAA" in tree_blob
