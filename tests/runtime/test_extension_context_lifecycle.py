"""docs/22 §9.1.5/6: call-time ExtensionContext + fail-loud staleness.

A context built from a host is valid while the host is bound; after invalidate()
(session replacement / teardown) the capability views fail loud instead of
writing the wrong session.
"""
import pytest

from nanocode.extensions import ExtensionHost
from nanocode.extensions.errors import ExtensionRuntimeError


class _FakeTaskManager:
    def __init__(self):
        self.updated = []

    def get_task(self, tid):
        return {"id": tid}

    def update_task(self, tid, **fields):
        self.updated.append((tid, fields))


class _FakeAgent:
    def __init__(self):
        self.task_manager = _FakeTaskManager()
        self.model = "claude-opus"
        self.events = []

    def emit(self, ev):
        self.events.append(ev)


class _FakeThread:
    def __init__(self):
        self._agent = _FakeAgent()
        self.model = "claude-opus"

    def readonly_session(self):
        return None


def _bound_host():
    host = ExtensionHost.load_system_extensions().activate_all()
    thread = _FakeThread()
    host.bind_runtime(thread, None)
    return host, thread


def test_context_is_live_while_bound():
    host, thread = _bound_host()
    ctx = host.create_context()
    assert ctx.memory is None              # services=None
    assert ctx.session is None
    ctx.tasks.update_task("t1", status="completed")
    assert thread._agent.task_manager.updated == [("t1", {"status": "completed"})]
    ctx.events.notice("hi")
    assert thread._agent.events  # NoticeRaised emitted


def test_stale_context_fails_loud_after_invalidate():
    host, thread = _bound_host()
    ctx = host.create_context()
    host.invalidate("dispose")
    with pytest.raises(ExtensionRuntimeError):
        ctx.tasks.update_task("t1", status="completed")
    with pytest.raises(ExtensionRuntimeError):
        ctx.models.resolve("memory_diagnosis")
    # raw thread / memory / session must not be reachable through a stale ctx
    with pytest.raises(ExtensionRuntimeError):
        _ = ctx.thread
    with pytest.raises(ExtensionRuntimeError):
        _ = ctx.memory
    with pytest.raises(ExtensionRuntimeError):
        _ = ctx.session


def test_create_context_after_invalidate_fails_loud():
    host, _thread = _bound_host()
    host.invalidate("dispose")
    with pytest.raises(ExtensionRuntimeError):
        host.create_context()


def test_command_context_has_wait_for_idle():
    host, _thread = _bound_host()
    cctx = host.create_command_context()
    assert cctx.wait_for_idle is not None
