import pytest

from nanocode.agent.engine import Agent
from nanocode.agent.events import ToolCallRequested, ToolResultObserved
from nanocode.runs.ledger import RunLedger
from nanocode.runs.runtime import AgentRunRuntime
from nanocode.session import tree as T
from nanocode.session.lease import SessionLease
from nanocode.session.manager import SessionManager, session_root
from nanocode.subagents import run_record
from nanocode.subagents.steer import drain_pending_steers, queue_steer


def _agent(sid="P"):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def _child(parent, child_id="CHILD"):
    parent._session_mgr = SessionManager.create(parent.session_id)
    spawn = parent._session_mgr.append_message(T.user_message("spawn child")).id
    parent_session = {"sessionId": parent.session_id, "entryId": spawn,
                      "taskId": child_id, "agentId": child_id}
    lease = SessionLease.open_or_create(child_id, parent_session=parent_session)
    lease.manager.rewrite_file()
    child = _agent(parent.session_id)
    child.is_sub_agent = True
    child._tree_session_id = child_id
    child._child_parent_session = parent_session
    child._session_lease = lease
    child._session_mgr = lease.manager
    return child, lease


def test_run_ledger_lists_only_session_header_children_not_orphan_sidecars():
    parent = _agent("PARENT")
    parent._session_mgr = SessionManager.create("PARENT")
    orphan = "ORPHAN"
    rd = session_root(orphan) / "subagent-run"
    rd.mkdir(parents=True)
    run_record.write_status(orphan, {
        "schemaVersion": 1,
        "runId": orphan,
        "childSessionId": orphan,
        "parentSessionId": "PARENT",
        "agentType": "coder",
        "description": "orphan",
        "status": "completed",
        "background": False,
        "contextMode": "fresh",
        "isolation": "shared",
        "model": {"provider": "anthropic", "modelId": "m"},
        "pendingSteerCount": 0,
    })

    assert RunLedger().list_for_parent("PARENT") == []


def test_pending_steer_drains_through_child_agent_session_record_event():
    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="coder",
            description="test run",
            background=True,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
        queued = queue_steer("CHILD", "narrow the search", delivery="steer")
        assert queued["state"] == "queued"

        assert drain_pending_steers(child, delivery="steer") == 1
        messages = [e.data["message"]["content"] for e in child._session_mgr.entries()
                    if e.type == T.MESSAGE]
        assert "narrow the search" in messages
        status = run_record.read_status("CHILD")
        assert status["pendingSteerCount"] == 0
    finally:
        lease.close()


def test_run_record_tracks_tool_activity_projection():
    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="coder",
            description="test run",
            background=True,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
        parent._spawn.attach_run_record_projector(child, "CHILD")

        child.emit(ToolCallRequested(
            tool="read_file",
            input={"file_path": "src/nanocode/tasks/runner.py"},
            tool_use_id="tu_read",
        ))
        running = run_record.read_status("CHILD")["metrics"]
        assert running["toolUses"] == 1
        assert running["currentTool"] == "read_file"
        assert running["activeTools"][0]["toolUseId"] == "tu_read"

        child.emit(ToolResultObserved(
            tool="read_file",
            tool_use_id="tu_read",
            chars=12,
            result="file body",
        ))
        done = run_record.read_status("CHILD")["metrics"]
        assert done["toolUses"] == 1
        assert done["activeTools"] == []
        assert done["currentTool"] is None
        events = run_record.read_events("CHILD")
        assert [e["type"] for e in events][-2:] == ["tool_started", "tool_finished"]
    finally:
        lease.close()


def test_run_record_persists_description_projection():
    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="explore",
            description="inspect subagent UI",
            background=True,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
        status = run_record.read_status("CHILD")
        rec = RunLedger().replay("CHILD")
        assert status["description"] == "inspect subagent UI"
        assert rec.description == "inspect subagent UI"
    finally:
        lease.close()


def test_runtime_subagent_conversation_snapshot_reads_child_session_messages():
    from nanocode.runtime import AgentRuntime

    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="explore",
            description="inspect transcript",
            background=True,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
        lease.manager.append_message(T.user_message("child prompt"))
        lease.manager.append_message(T.assistant_message(
            "child answer",
            provider="anthropic",
            api="messages",
            model="m",
            stop_reason="end_turn",
        ))
    finally:
        lease.close()

    thread = AgentRuntime()._attach_agent(parent)
    snapshot = thread.subagent_conversation_snapshot("CHILD")

    assert snapshot["record"]["description"] == "inspect transcript"
    assert [m["role"] for m in snapshot["messages"]] == ["user", "assistant"]
    assert snapshot["messages"][0]["content"] == "child prompt"


def test_terminal_steer_rejected_use_resume():
    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="coder",
            description="test run",
            background=False,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
        run_record.complete_run(
            "CHILD", status="completed", result="done",
            prompt_entry_id=None, result_entry_id=None)
    finally:
        lease.close()

    with pytest.raises(RuntimeError, match="use resume"):
        AgentRunRuntime().send("CHILD", "change scope", delivery="steer")


def test_rebind_marks_nonterminal_run_without_live_runner_lost():
    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="coder",
            description="test run",
            background=True,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
    finally:
        lease.close()

    records = AgentRunRuntime().rebind("PARENT", live_run_ids=set())
    assert [(r.child_session_id, r.status) for r in records] == [("CHILD", "lost")]
    status = run_record.read_status("CHILD")
    assert status["status"] == "lost"
    assert any(e["type"] == "lost" for e in run_record.read_events("CHILD"))


def test_run_cancel_without_live_coroutine_marks_lost_not_cancelled():
    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="coder",
            description="test run",
            background=True,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
    finally:
        lease.close()

    import asyncio
    res = asyncio.run(parent.run_cancel("CHILD"))
    assert "marked lost" in res
    assert run_record.read_status("CHILD")["status"] == "lost"


def test_list_is_readonly_view_does_not_persist_lost():
    """A4a (docs/25)：重绘/列举 list() 对「非终态 + 无 live coroutine」的 run 只在内存里显示
    lost，不落盘、不发事件；显式 rebind() 才持久化 lost。"""
    parent = _agent("PARENT")
    child, lease = _child(parent, "CHILD")
    try:
        run_record.create_run_record(
            child_session_id="CHILD",
            parent_session_id="PARENT",
            spawn_entry_id=parent._session_mgr.get_leaf(),
            tool_call_id=None,
            agent_type="coder",
            description="test run",
            background=True,
            context_mode="fresh",
            isolation="shared",
            worktree_path=None,
            model={"provider": "anthropic", "modelId": "m"},
            prompt="initial",
        )
    finally:
        lease.close()

    rt = AgentRunRuntime()
    events_before = len(run_record.read_events("CHILD"))
    for _ in range(5):  # 多次重绘
        recs = rt.list("PARENT", live_run_ids=set())
        assert [(r.child_session_id, r.status) for r in recs] == [("CHILD", "lost")]
    # 视图显示 lost，但磁盘仍非终态（未写 lost），且零新事件（重绘零写）
    assert run_record.read_status("CHILD")["status"] == "running"
    assert not any(e["type"] == "lost" for e in run_record.read_events("CHILD"))
    assert len(run_record.read_events("CHILD")) == events_before
    # 显式 rebind 才持久化 lost
    rt.rebind("PARENT", live_run_ids=set())
    assert run_record.read_status("CHILD")["status"] == "lost"
