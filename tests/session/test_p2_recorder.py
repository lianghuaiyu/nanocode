"""P2 TurnRecorder：tail-append 双写、resume 对齐、compaction-shrink 跳过、turn-end leaf。"""

from nanocode.session import tree
from nanocode.session.manager import SessionManager
from nanocode.session.recorder import TurnRecorder

A_USER = {"role": "user", "content": "q1"}
A_ASST = {"role": "assistant", "content": [{"type": "text", "text": "a1"}]}


def _msg_count(sid):
    return sum(1 for e in SessionManager.open(sid).entries() if e.type == tree.MESSAGE)


def test_record_turn_tail_appends_across_turns():
    rec = TurnRecorder("r1")
    rec.record_turn("anthropic", [A_USER, A_ASST], model="claude-x")
    assert _msg_count("r1") == 2
    # 第二轮：列表增长，只 append 新增 tail（不重复前两条）
    rec.record_turn("anthropic", [A_USER, A_ASST, {"role": "user", "content": "q2"},
                                  {"role": "assistant", "content": [{"type": "text", "text": "a2"}]}],
                    model="claude-x")
    assert _msg_count("r1") == 4


def test_resume_alignment_no_duplicate():
    rec1 = TurnRecorder("r2")
    rec1.record_turn("anthropic", [A_USER, A_ASST], model="claude-x")
    # 新进程/新 recorder 接续同一 session：按已有 message 数对齐，不重复 append
    rec2 = TurnRecorder("r2")
    rec2.record_turn("anthropic", [A_USER, A_ASST], model="claude-x")
    assert _msg_count("r2") == 2


def test_compaction_shrink_skipped():
    rec = TurnRecorder("r3")
    rec.record_turn("anthropic", [A_USER, A_ASST, {"role": "user", "content": "q2"}], model="claude-x")
    assert _msg_count("r3") == 3
    # 列表被 compaction 整列替换变短 → 跳过，不破坏既有树
    rec.record_turn("anthropic", [{"role": "user", "content": "[summary]"}], model="claude-x")
    assert _msg_count("r3") == 3


def test_turn_end_leaf_marker_and_leaf_advances():
    rec = TurnRecorder("r4")
    rec.record_turn("anthropic", [A_USER, A_ASST], model="claude-x")
    m = SessionManager.open("r4")
    assert any(e.type == tree.LEAF for e in m.entries())   # turn-end leaf 标记
    branch = m.build_context().messages
    assert [mm["role"] for mm in branch] == ["user", "assistant"]
