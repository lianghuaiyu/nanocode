"""P1 tree + context 单测：leaf 折叠、get_branch、labels/name LWW、fold 标量+两区、convert_to_llm。"""

from nanocode.session import context, tree
from nanocode.session.tree import Entry


def _e(eid, parent, etype, **data):
    return Entry(type=etype, id=eid, parentId=parent, sessionId="s", timestamp="2026-01-01T00:00:00Z", data=data)


# ─── leaf_id_after_entry / current_leaf ──────────────────────────────────────
def test_leaf_rule_table():
    assert tree.leaf_id_after_entry(_e("m1", None, tree.MESSAGE, message={})) == "m1"
    assert tree.leaf_id_after_entry(_e("L1", "m1", tree.LEAF, targetId="m0")) == "m0"
    assert tree.leaf_id_after_entry(_e("L2", "m1", tree.LEAF, targetId=None)) is None
    assert tree.leaf_id_after_entry(_e("lab", "m1", tree.LABEL, targetId="m1", label="x")) is tree._UNCHANGED
    assert tree.leaf_id_after_entry(_e("si", "m1", tree.SESSION_INFO, name="n")) is tree._UNCHANGED


def test_current_leaf_fold_last_wins_and_unchanged_skipped():
    entries = [
        _e("r", None, tree.SESSION_START),
        _e("m1", "r", tree.MESSAGE, message={}),
        _e("lab", "m1", tree.LABEL, targetId="m1", label="bookmark"),  # 不移 leaf
        _e("L", "lab", tree.LEAF, targetId="r"),                       # 回到 root
    ]
    assert tree.current_leaf(entries) == "r"
    assert tree.current_leaf([]) is None


def test_get_branch_leaf_to_root_root_first():
    by_id = tree.index_by_id([
        _e("r", None, tree.SESSION_START),
        _e("a", "r", tree.MESSAGE, message={}),
        _e("b", "a", tree.MESSAGE, message={}),
        _e("side", "r", tree.MESSAGE, message={}),  # sibling branch，不在 b 的 path 上
    ])
    branch = tree.get_branch(by_id, "b")
    assert [e.id for e in branch] == ["r", "a", "b"]  # root-first
    assert [e.id for e in tree.get_branch(by_id, "side")] == ["r", "side"]
    assert tree.get_branch(by_id, None) == []


def test_labels_lww_tombstone_and_name():
    entries = [
        _e("lab1", None, tree.LABEL, targetId="x", label="first"),
        _e("lab2", "lab1", tree.LABEL, targetId="x", label="second"),  # LWW
        _e("lab3", "lab2", tree.LABEL, targetId="y", label="ylabel"),
        _e("lab4", "lab3", tree.LABEL, targetId="y", label="  "),      # tombstone
        _e("si1", "lab4", tree.SESSION_INFO, name="My Session"),
        _e("si2", "si1", tree.SESSION_INFO, name=""),                  # 清空
    ]
    assert tree.labels_by_id(entries) == {"x": "second"}
    assert tree.session_name(entries) is None
    assert tree.session_name(entries[:5]) == "My Session"


# ─── fold ────────────────────────────────────────────────────────────────────
def test_fold_scalar_lww_model_from_change_and_assistant():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("mc", "r", tree.MODEL_CHANGE, provider="anthropic", modelId="claude-x"),
        _e("th", "mc", tree.THINKING_LEVEL_CHANGE, thinkingLevel="high"),
        _e("at", "th", tree.ACTIVE_TOOLS_CHANGE, activeToolNames=["read_file"]),
        _e("a", "at", tree.MESSAGE, message=tree.assistant_message(
            [tree.text_block("hi")], provider="openai", api="openai-completions",
            model="gpt-x", stop_reason="stop")),
    ]
    _, scalar = context.fold(branch)
    assert scalar.thinking_level == "high"
    assert scalar.active_tools == ["read_file"]
    # assistant 消息记录的 provider/model 末者胜（覆盖 model_change）
    assert scalar.provider == "openai" and scalar.model_id == "gpt-x"


def test_fold_compaction_two_regions():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("u1", "r", tree.MESSAGE, message=tree.user_message("old1")),    # 区一、compaction 前、firstKept 前 → 丢
        _e("u2", "u1", tree.MESSAGE, message=tree.user_message("kept")),   # firstKeptEntryId → 保
        _e("c", "u2", tree.COMPACTION, summary="SUMMARY", firstKeptEntryId="u2", tokensBefore=999),
        _e("u3", "c", tree.MESSAGE, message=tree.user_message("after")),   # 区二 → 保
    ]
    rich, _ = context.fold(branch)
    roles_text = [(m.get("role"), m.get("summary") or m.get("content")) for m in rich]
    assert roles_text[0] == ("compactionSummary", "SUMMARY")
    contents = [m.get("content") for m in rich if m.get("role") == "user"]
    assert contents == ["kept", "after"]  # old1 被 compaction 前区裁掉


def test_fold_branch_summary_injected_when_nonempty():
    branch = [
        _e("r", None, tree.SESSION_START),
        _e("bs", "r", tree.BRANCH_SUMMARY, summary="explored X", fromId="r"),
        _e("bs0", "bs", tree.BRANCH_SUMMARY, summary="   "),  # 空 → 不注入
    ]
    rich, _ = context.fold(branch)
    bs = [m for m in rich if m.get("role") == "branchSummary"]
    assert len(bs) == 1 and bs[0]["summary"] == "explored X"


# ─── convert_to_llm ────────────────────────────────────────────────────────────
def test_convert_to_llm_prefix_vs_verbatim():
    rich = [
        {"role": "compactionSummary", "summary": "S"},
        {"role": "branchSummary", "summary": "B"},
        {"role": "custom", "customType": "skill_listing", "content": "<system-reminder>x</system-reminder>"},
        tree.user_message("u"),
        tree.assistant_message([tree.text_block("a")], provider="anthropic", api="anthropic",
                               model="claude-x", stop_reason="stop"),
    ]
    out = context.convert_to_llm(rich)
    assert out[0]["role"] == "user" and out[0]["content"].startswith(context.COMPACTION_PREFIX)
    assert out[1]["content"].startswith(context.BRANCH_SUMMARY_PREFIX)
    # custom_message 原样、无 PREFIX（否则改写注入文本）
    assert out[2]["role"] == "user" and out[2]["content"] == "<system-reminder>x</system-reminder>"
    assert out[3]["content"] == "u"
    assert out[4]["role"] == "assistant"
