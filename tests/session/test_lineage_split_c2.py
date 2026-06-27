"""docs/26 阶段2 C2：session header 血缘正交 spawnedBy ⟂ forkedFrom。

核心纠偏：children() 只认 spawnedBy（subagent 控制血缘），fork/clone（forkedFrom 内容血缘）
不再被误当 subagent 子；两类血缘各有独立访问器；get_latest 仅排除 spawnedBy。
"""
from nanocode.session.manager import SessionManager, children, parent_of
from nanocode.session.store import get_latest_session_id


def test_children_match_spawned_by_only_not_fork():
    SessionManager.create("P_C2").close()
    SessionManager.create(
        "P_C2.sub",
        spawned_by={"sessionId": "P_C2", "entryId": "e", "taskId": "P_C2.sub", "agentId": "P_C2.sub"},
    ).close()
    SessionManager.create(
        "P_C2.fork",
        forked_from={"sessionId": "P_C2", "entryId": "e", "forkedBeforeEntryId": "u1"},
    ).close()

    # 仅 spawnedBy 子进 children()；fork 不进（否则 run-ledger 会把 fork 误当 subagent 运行）。
    assert children("P_C2") == ["P_C2.sub"]
    assert parent_of("P_C2.sub") == "P_C2"
    assert parent_of("P_C2.fork") is None     # fork 不是控制子


def test_accessors_are_orthogonal():
    sub = SessionManager.create("ACC.sub", spawned_by={"sessionId": "ACC", "agentId": "ACC.sub"})
    assert sub.spawned_by() == {"sessionId": "ACC", "agentId": "ACC.sub"}
    assert sub.forked_from() is None

    fork = SessionManager.create("ACC.fork", forked_from={"sessionId": "ACC", "forkedBeforeEntryId": "u1"})
    assert fork.forked_from() == {"sessionId": "ACC", "forkedBeforeEntryId": "u1"}
    assert fork.spawned_by() is None

    root = SessionManager.create("ACC.root")
    assert root.spawned_by() is None and root.forked_from() is None


def test_get_latest_excludes_spawned_but_allows_fork():
    import time
    SessionManager.create("LROOT").close()
    time.sleep(0.01)
    # fork 比 root 更晚 → 可作 latest（forkedFrom 不排除）。
    SessionManager.create("LFORK", forked_from={"sessionId": "LROOT", "forkedBeforeEntryId": "u1"}).close()
    time.sleep(0.01)
    # subagent 最晚但被排除。
    SessionManager.create("LSUB", spawned_by={"sessionId": "LROOT", "agentId": "LSUB"}).close()

    latest = get_latest_session_id()
    assert latest == "LFORK"      # fork 可作 latest；subagent 被排除
