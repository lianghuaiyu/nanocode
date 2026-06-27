"""docs/26 阶段2 D3：后台子 agent 危险确认升级回父 + 父侧 allow/deny 应答。

- 后台子触发危险确认 → 不再 auto-deny，而是发父可见 NoticeRaised + 写 run_record pendingApproval
  + 阻塞 await 父应答；
- 父 run_approve(allow/deny) 解阻塞，子据此放行/拒绝；
- run_cancel 取消 await 中的子不泄漏 _pending_approvals / sidecar；
- reserved/curator 路径不升级（保持 auto-deny）；
- ConversationModel 仅在 pending 时给 a/d 应答键。
"""
import asyncio
import re

from nanocode.agent.engine import Agent
from nanocode.agent.events import NoticeRaised
from nanocode.runs.models import TERMINAL_RUN_STATUSES
from nanocode.runtime.spawn import _auto_deny_confirm
from nanocode.subagents import run_record


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="d3sid", **kw)


def _spy_build(parent, *, run_once):
    built = {}
    real = parent._build_sub_agent

    def _spy(**kw):
        sub = real(**kw)
        built["sub"] = sub
        sub.run_once = run_once(sub)
        return sub

    parent._build_sub_agent = _spy
    return built


def _confirm_then_report(sub):
    async def _ro(prompt: str) -> dict:
        decision = await sub.confirm_fn("rm -rf /tmp/x")
        text = "approved-did-it" if decision else "denied-skipped"
        if sub._session_mgr is not None:
            sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
            sub.agent_session.record_provider_messages({"role": "assistant", "content": text})
        return {"text": text, "tokens": {"input": 1, "output": 1}}
    return _ro


def _run_id(text: str) -> str:
    m = re.search(r"run (sess_[A-Za-z0-9_]+)", text)
    assert m, text
    return m.group(1)


async def _wait_terminal(run_id, tries=300, delay=0.01):
    for _ in range(tries):
        try:
            st = run_record.read_status(run_id)
        except FileNotFoundError:
            st = None
        if st and st["status"] in TERMINAL_RUN_STATUSES:
            return st
        await asyncio.sleep(delay)
    return run_record.read_status(run_id)


async def _wait_pending(run_id, tries=300, delay=0.01):
    for _ in range(tries):
        try:
            st = run_record.read_status(run_id)
        except FileNotFoundError:
            st = None
        if st and st.get("pendingApproval"):
            return st["pendingApproval"]
        await asyncio.sleep(delay)
    return None


def test_background_escalates_and_parent_approves():
    parent = _agent()
    _spy_build(parent, run_once=_confirm_then_report)
    notices = []
    parent._event_subscribers.append(
        lambda e: notices.append(e) if isinstance(e, NoticeRaised) else None)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        run_id = _run_id(res)
        pa = await _wait_pending(run_id)
        assert pa and pa["command"] == "rm -rf /tmp/x"
        # 升级期间发了父可见 NoticeRaised（非 modal）
        assert any("requests approval" in n.text for n in notices)
        msg = parent.run_approve(run_id, True)
        assert "Approved" in msg
        return run_id, await _wait_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "completed"
    assert "approved-did-it" in run_record.read_result(run_id)
    assert status["pendingApproval"] is None          # 应答后清除
    assert parent._pending_approvals == {}             # registry 无泄漏


def test_background_escalates_and_parent_denies():
    parent = _agent()
    _spy_build(parent, run_once=_confirm_then_report)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        run_id = _run_id(res)
        await _wait_pending(run_id)
        parent.run_approve(run_id, False)
        return run_id, await _wait_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "completed"
    assert "denied-skipped" in run_record.read_result(run_id)
    assert parent._pending_approvals == {}


def test_run_cancel_while_awaiting_approval_no_leak():
    parent = _agent()
    entered = asyncio.Event()

    def _await_forever(sub):
        async def _ro(prompt):
            entered.set()
            decision = await sub.confirm_fn("dangerous")   # blocks until approved/cancelled
            return {"text": "done" if decision else "denied", "tokens": {"input": 0, "output": 0}}
        return _ro

    _spy_build(parent, run_once=_await_forever)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        run_id = _run_id(res)
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        await _wait_pending(run_id)
        await parent.run_cancel(run_id)
        return run_id, await _wait_terminal(run_id)

    run_id, status = asyncio.run(scenario())
    assert status["status"] == "cancelled"
    assert status["pendingApproval"] is None
    assert parent._pending_approvals == {}


def test_background_path_overrides_auto_deny_with_escalation():
    parent = _agent()
    built = _spy_build(parent, run_once=_confirm_then_report)

    async def scenario():
        res = await parent._execute_agent_tool(
            {"type": "coder", "description": "d", "prompt": "p", "run_in_background": True})
        run_id = _run_id(res)
        await _wait_pending(run_id)
        parent.run_approve(run_id, True)
        await _wait_terminal(run_id)

    asyncio.run(scenario())
    # 后台用户子 agent 的 confirm_fn 被覆盖为升级闭包（非 auto-deny），且共享父去重集。
    assert built["sub"].confirm_fn is not _auto_deny_confirm
    assert built["sub"]._confirmed_paths is parent._confirmed_paths


def test_run_approve_noop_without_pending():
    parent = _agent()
    assert "no pending approval" in parent.run_approve("sess_nope", True)


def test_run_record_pending_approval_roundtrip():
    parent = _agent()
    # 直接驱动 sidecar 助手（create→set→clear）
    sub = parent._build_sub_agent(system_prompt="s", tools=[], agent_type="coder", background=True,
                                  artifact_id="sess_pa_rt")
    cid = sub._tree_session_id
    parent._spawn.begin_run_record(
        parent, sub_agent=sub, agent_id=cid, agent_type="coder", description="d",
        prompt="p", model="m", background=True, context_mode="fresh",
        isolation="shared", worktree_path=None)
    assert run_record.read_status(cid)["pendingApproval"] is None
    run_record.set_pending_approval(cid, approval_id="ab12", command="rm x")
    assert run_record.read_status(cid)["pendingApproval"] == {"approvalId": "ab12", "command": "rm x"}
    run_record.clear_pending_approval(cid)
    assert run_record.read_status(cid)["pendingApproval"] is None


# ─── /agents ConversationModel a/d 应答键 ────────────────────────────────────

def _conv_model(pending):
    from nanocode.tui.session_pages.agents import ConversationModel
    record = {
        "status": "running", "agent_type": "coder", "description": "d",
        "child_session_id": "sess_x", "metrics": {}, "pending_approval": pending,
    }
    return ConversationModel({"record": record, "messages": []})


def test_conversation_model_offers_allow_deny_only_when_pending():
    m = _conv_model({"approvalId": "a1", "command": "rm x"})
    assert "a" in m.extra_keys() and "d" in m.extra_keys()
    assert m.on_key("a", "", 0).edit_action == "approve"
    assert m.on_key("d", "", 0).edit_action == "deny"

    m2 = _conv_model(None)
    assert "a" not in m2.extra_keys() and "d" not in m2.extra_keys()
    assert m2.on_key("a", "", 0) is None
