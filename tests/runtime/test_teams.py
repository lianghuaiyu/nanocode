"""docs/15 Phase 8 §12：TeamRuntime 骨架 + claim lock + 预留 team session entry 类型。

验收：能创建带 task board 的 team session;claim lock 保证单认领（无双认领）;agent-to-agent 通信
走 mailbox/team entry,不进父 transcript;预留状态模型完整。
"""

from nanocode.runtime import (
    AgentMailbox, ClaimLock, SharedArtifactStore, TeamEventStream, TeamRuntime, TeamSession, TeamTaskBoard,
)
from nanocode.session import tree as T


def test_reserved_team_entry_types_exist_and_non_leaf_affecting():
    for et in (T.TEAM_START, T.TEAM_TASK_UPDATE, T.TEAM_MESSAGE, T.TEAM_CLAIM,
               T.TEAM_RESULT, T.AGENT_MAILBOX_MESSAGE):
        assert et in T.TEAM_ENTRY_TYPES
        # team entry 非 FOLD_TYPES（对 LLM 不可见）
        assert et not in T.FOLD_TYPES
        # 非 leaf-affecting（注解型,不推进对话 branch）
        e = T.Entry(type=et, id="x", parentId=None, sessionId="s", timestamp="t", data={})
        assert T.leaf_id_after_entry(e) is T._UNCHANGED


def test_create_team_with_task_board():
    rt = TeamRuntime()
    ts = rt.create_team(["a1", "a2"])
    assert isinstance(ts, TeamSession)
    assert ts.members == ["a1", "a2"]
    assert isinstance(ts.board, TeamTaskBoard)
    assert rt.team(ts.team_id) is ts
    # team_start 事件已发
    assert any(e["kind"] == T.TEAM_START for e in ts.events.events())


def test_claim_lock_prevents_double_claim():
    lock = ClaimLock()
    assert lock.claim("t1", "a1") is True
    assert lock.claim("t1", "a2") is False        # 已被 a1 认领
    assert lock.claim("t1", "a1") is True         # 自己幂等
    assert lock.owner("t1") == "a1"
    assert lock.release("t1", "a2") is False       # 非 owner 不能释放
    assert lock.release("t1", "a1") is True
    assert lock.claim("t1", "a2") is True          # 释放后可被他人认领


def test_task_board_claim_single_owner():
    board = TeamTaskBoard()
    t = board.add("investigate bug")
    assert t.status == "open" and t.owner is None
    assert board.claim(t.id, "a1") is True
    assert board.get(t.id).owner == "a1" and board.get(t.id).status == "claimed"
    assert board.claim(t.id, "a2") is False        # 单认领
    board.update(t.id, status="done", result="fixed")
    assert board.get(t.id).status == "done" and board.get(t.id).result == "fixed"
    assert board.claim("nonexistent", "a1") is False


def test_mailbox_agent_to_agent_not_in_transcript():
    mb = AgentMailbox()
    mb.send("a1", "a2", "please take task tt1")
    inbox = mb.inbox("a2")
    assert len(inbox) == 1 and inbox[0].sender == "a1" and inbox[0].body == "please take task tt1"
    assert mb.inbox("a1") == []                    # a1 收件箱空
    drained = mb.drain("a2")
    assert len(drained) == 1 and mb.inbox("a2") == []


def test_shared_artifacts_and_events():
    store = SharedArtifactStore()
    store.put("plan.md", "step 1...")
    assert store.get("plan.md") == "step 1..." and "plan.md" in store.keys()
    es = TeamEventStream()
    es.emit(T.TEAM_CLAIM, task="tt1", agent="a1")
    assert es.events()[0]["kind"] == T.TEAM_CLAIM


def test_team_session_aggregates_and_adds_members():
    ts = TeamSession(team_id="teamX")
    ts.add_member("a1")
    ts.add_member("a1")                            # 幂等
    assert ts.members == ["a1"]
    ts.board.add("task")
    assert len(ts.board.list()) == 1
