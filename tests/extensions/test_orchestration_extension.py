"""docs/26 §0.6 阶段1：orchestration 扩展 + 受信编排槽（O5）契约。

- orchestrator 唯一注册（dup fail-loud）；host.run_orchestrator 无注册 → fail-loud；
- SpawnCap 编排原语 gated on `spawn:orchestrate`（未授予 → raise）；拒 reserved 类型（非提权）；
- 系统 host 把 orchestration 扩展授予 `spawn:orchestrate`（_orchestrate_granted）。

fg chain/parallel 与后台编排的端到端行为由 tests/agent/test_agent_chain_parallel.py 与
test_background_orchestration.py 经扩展路径覆盖（行为保真）。本文件只验扩展边界契约。
"""
import asyncio

import pytest

from nanocode.extensions import ExtensionHost
from nanocode.extensions.context import SpawnCap
from nanocode.extensions.errors import ExtensionLoadError, ExtensionRuntimeError
from nanocode.extensions.registry import ContributionRegistry


class _ActiveHost:
    is_active = True


class _FakeThread:
    def __init__(self):
        self._agent = None
        self.model = "claude-opus"
        self.calls = []

    def readonly_session(self):
        return None

    def new_orchestration_group(self):
        return "orch_test"

    async def run_orchestration_member(self, agent_type, prompt, **kw):
        self.calls.append(("fresh", agent_type, prompt, kw))
        return f"env:{agent_type}"

    async def run_orchestration_step(self, agent_type, prompt, **kw):
        self.calls.append(("step", agent_type, prompt, kw))
        return f"text:{agent_type}"

    async def spawn_orchestration_background(self, agent_type, prompt, **kw):
        self.calls.append(("bg", agent_type, prompt, kw))
        return "sess_x"

    async def cancel_runs(self, group_id):
        self.calls.append(("cancel", group_id))
        return "cancelled"

    def list_children(self, *, status=None):
        self.calls.append(("list", status))
        return [{"child_session_id": "sess_x", "status": status or "running"}]

    def child_status(self, child_session_id):
        self.calls.append(("status", child_session_id))
        return {"child_session_id": child_session_id, "status": "running"}

    def child_result(self, child_session_id):
        self.calls.append(("result", child_session_id))
        return {"childSessionId": child_session_id, "status": "completed"}

    async def wait_child(self, child_session_id, **kw):
        self.calls.append(("wait", child_session_id, kw))
        return {"child_session_id": child_session_id, "status": "completed"}

    async def cancel_child(self, child_session_id):
        self.calls.append(("cancel_child", child_session_id))
        return "child cancelled"

    def steer_child(self, child_session_id, prompt, **kw):
        self.calls.append(("steer", child_session_id, prompt, kw))
        return {"state": "queued"}

    def approval_inbox(self):
        self.calls.append(("approval_inbox",))
        return [{"childSessionId": "sess_x", "approval": {"approvalId": "ap1"}}]

    def approve_child(self, child_session_id, approved):
        self.calls.append(("approve", child_session_id, approved))
        return "approved" if approved else "denied"

    def launch_orchestration_coordinator(self, coro, *, group_id):
        coro.close()   # 测试不真正跑 coordinator
        self.calls.append(("launch", group_id))


def _cap(*, can_orchestrate, thread=None):
    return SpawnCap(_ActiveHost(), thread or _FakeThread(),
                    allowed_agent_types=frozenset(), can_orchestrate=can_orchestrate)


# ─── 注册契约 ──────────────────────────────────────────────────────────────────

def test_orchestrator_registered_uniquely_in_system_host():
    h = ExtensionHost.load_system_extensions().activate_all()
    assert h.registry.orchestrator is not None
    assert h.registry.orchestrator[1] == "nanocode.orchestration"
    assert h._orchestrate_granted() is True


def test_duplicate_orchestrator_fails_loud():
    reg = ContributionRegistry()

    async def _h(ctx, payload):
        return "x"

    reg.add_orchestrator(_h, extension_id="a")
    with pytest.raises(ExtensionLoadError):
        reg.add_orchestrator(_h, extension_id="b")


def test_run_orchestrator_without_registration_fails_loud():
    h = ExtensionHost([])
    h._activated = True
    h.bind_runtime(_FakeThread(), None)
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(h.run_orchestrator({"tasks": [{"prompt": "x"}]}))


# ─── 受信编排槽（O5）─────────────────────────────────────────────────────────────

def test_orchestrate_methods_gated_when_not_granted():
    cap = _cap(can_orchestrate=False)
    with pytest.raises(ExtensionRuntimeError):
        cap.new_group()
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(cap.run_fresh("coder", "p"))
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(cap.run_step("coder", "p", group_id="g"))
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(cap.run_background("coder", "p"))
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(cap.cancel_group("g"))
    with pytest.raises(ExtensionRuntimeError):
        cap.list_children()
    with pytest.raises(ExtensionRuntimeError):
        cap.child_status("sess_x")
    with pytest.raises(ExtensionRuntimeError):
        cap.child_result("sess_x")
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(cap.wait_child("sess_x"))
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(cap.cancel_child("sess_x"))
    with pytest.raises(ExtensionRuntimeError):
        cap.steer_child("sess_x", "p")


def test_orchestrate_methods_delegate_to_kernel_when_granted():
    thread = _FakeThread()
    cap = _cap(can_orchestrate=True, thread=thread)
    assert cap.new_group() == "orch_test"
    assert asyncio.run(cap.run_fresh("coder", "p", description="d")) == "env:coder"
    assert asyncio.run(cap.run_step("coder", "p", group_id="g")) == "text:coder"
    assert asyncio.run(cap.run_background("coder", "p", group_id="g")) == "sess_x"
    assert asyncio.run(cap.cancel_group("g")) == "cancelled"
    kinds = [c[0] for c in thread.calls]
    assert kinds == ["fresh", "step", "bg", "cancel"]


def test_orchestrate_child_control_methods_delegate_to_runtime_facade():
    thread = _FakeThread()
    cap = _cap(can_orchestrate=True, thread=thread)
    assert cap.list_children(status="running") == [
        {"child_session_id": "sess_x", "status": "running"}]
    assert cap.child_status("sess_x")["status"] == "running"
    assert cap.child_result("sess_x")["status"] == "completed"
    assert asyncio.run(cap.wait_child("sess_x", timeout_ms=10))["status"] == "completed"
    assert asyncio.run(cap.cancel_child("sess_x")) == "child cancelled"
    assert cap.steer_child("sess_x", "adjust") == {"state": "queued"}
    kinds = [c[0] for c in thread.calls]
    assert kinds == ["list", "status", "result", "wait", "cancel_child", "steer"]


def test_orchestrate_context_exposes_approval_and_workspace_views():
    thread = _FakeThread()
    h = ExtensionHost.load_system_extensions().activate_all()
    h.bind_runtime(thread, None)
    ctx = h.create_context()
    assert ctx.approvals.pending()[0]["childSessionId"] == "sess_x"
    assert ctx.approvals.decide("sess_x", True) == "approved"
    assert ctx.workspace.supported_modes() == ["shared", "worktree"]
    resolved = ctx.workspace.resolve(agent_type="coder", parallel=True)
    assert resolved["provider"] == "nanocode.default"
    assert resolved["mode"] in {"shared", "worktree"}
    assert [c[0] for c in thread.calls[:2]] == ["approval_inbox", "approve"]


def test_context_without_orchestrate_capability_has_no_approval_or_workspace_views():
    h = ExtensionHost([])
    h._activated = True
    h.bind_runtime(_FakeThread(), None)
    ctx = h.create_context()
    assert ctx.spawn is None
    assert ctx.approvals is None
    assert ctx.workspace is None


def test_orchestrate_rejects_reserved_agent_type():
    from nanocode.agents.registry import RESERVED_AGENT_TYPES
    reserved = next(iter(RESERVED_AGENT_TYPES))
    cap = _cap(can_orchestrate=True)
    for coro_factory in (
        lambda: cap.run_fresh(reserved, "p"),
        lambda: cap.run_step(reserved, "p", group_id="g"),
        lambda: cap.run_background(reserved, "p"),
    ):
        with pytest.raises(ExtensionRuntimeError):
            asyncio.run(coro_factory())
