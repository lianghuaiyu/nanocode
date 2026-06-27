"""docs/26 阶段1 ③：subagent lifecycle 充实事件 + 稳定 typed 扩展事件桥。

- spawn 路径发出的 SubAgentStarted/Ended 携带 run_id/child_session_id/status/tokens；
- 此前零调用方的扩展 lifecycle 通道（api.on / ExtensionHost.emit）真正投递，handler 异常隔离；
- RuntimeThread._on_agent_event 把 sub_agent_* 桥接成 "subagent.started/ended" 扩展事件
  （fire-and-forget；事件名故意与内部 kind 解耦）。
"""
import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.events import SubAgentEnded, SubAgentStarted
from nanocode.agents.registry import build_profile
from nanocode.extensions import ExtensionHost
from nanocode.runtime import AgentRuntime


# ─── ③.1 充实事件：spawn 路径发出的事件带 run_id/status ─────────────────────────

def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    kw.setdefault("session_id", "life_parent")
    return Agent(api_key="test", **kw)


def _stub(parent, *, text):
    real = parent._build_sub_agent

    def _spy(**kw):
        sub = real(**kw)

        async def _ro(prompt):
            if sub._session_mgr is not None:
                sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
                sub.agent_session.record_provider_messages({"role": "assistant", "content": text})
            return {"text": text, "tokens": {"input": 3, "output": 4}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


def test_spawn_emits_enriched_lifecycle_events():
    parent = _agent()
    _stub(parent, text="ok")
    seen = []
    parent._event_subscribers.append(seen.append)

    asyncio.run(parent._spawn_subagent(profile=build_profile("coder"), prompt="p"))

    started = [e for e in seen if isinstance(e, SubAgentStarted)]
    ended = [e for e in seen if isinstance(e, SubAgentEnded)]
    assert len(started) == 1 and len(ended) == 1
    assert started[0].run_id and started[0].run_id == started[0].child_session_id
    assert started[0].background is True
    assert ended[0].run_id == started[0].run_id
    assert ended[0].status == "completed"
    assert ended[0].tokens == {"input": 3, "output": 4}


# ─── ③.2a 扩展 lifecycle 通道真正投递（此前零调用方）────────────────────────────

class _FakeThread:
    _agent = None
    model = "claude-x"

    def readonly_session(self):
        return None


def test_extension_lifecycle_channel_delivers_and_isolates_errors():
    host = ExtensionHost([]).activate_all()
    host.bind_runtime(_FakeThread(), None)
    got = []

    async def good(ctx, payload):
        got.append(payload)

    async def bad(ctx, payload):
        raise RuntimeError("handler bug")

    host.registry.add_lifecycle("subagent.started", bad, extension_id="x")
    host.registry.add_lifecycle("subagent.started", good, extension_id="x")

    asyncio.run(host.emit("subagent.started", {"run_id": "r1", "agent_type": "coder"}))
    # 坏 handler 被隔离（不抛），好 handler 仍收到 payload。
    assert got == [{"run_id": "r1", "agent_type": "coder"}]


def test_extension_lifecycle_channel_noop_without_handlers():
    host = ExtensionHost([]).activate_all()
    host.bind_runtime(_FakeThread(), None)
    # 无订阅者 → 即刻返回，不构造 ctx、不报错。
    asyncio.run(host.emit("subagent.ended", {"run_id": "r1"}))


# ─── ③.2b facade 桥：sub_agent_* → "subagent.*" 扩展事件 ────────────────────────

class _RecordingHost:
    is_active = True

    def __init__(self):
        self.emitted = []

    async def emit(self, name, payload):
        self.emitted.append((name, payload))


def test_facade_bridges_subagent_events_to_extension_host():
    a = _agent(session_id="bridge_parent")
    thread = AgentRuntime()._attach_agent(a)
    fake = _RecordingHost()
    thread._extension_host = fake

    async def scenario():
        a.emit(SubAgentStarted(agent_type="coder", description="d",
                               run_id="r9", child_session_id="r9", background=True))
        a.emit(SubAgentEnded(agent_type="coder", description="d", run_id="r9",
                             child_session_id="r9", status="completed",
                             tokens={"input": 1, "output": 2}))
        await asyncio.sleep(0)  # 让 create_task 调度的 host.emit 跑完

    asyncio.run(scenario())

    names = [n for n, _ in fake.emitted]
    assert names == ["subagent.started", "subagent.ended"]
    started_payload = fake.emitted[0][1]
    assert started_payload["run_id"] == "r9" and started_payload["background"] is True
    ended_payload = fake.emitted[1][1]
    assert ended_payload["status"] == "completed" and ended_payload["tokens"] == {"input": 1, "output": 2}


def test_facade_bridge_skips_when_host_inactive():
    a = _agent(session_id="bridge_parent2")
    thread = AgentRuntime()._attach_agent(a)

    class _Inactive:
        is_active = False

        async def emit(self, name, payload):  # pragma: no cover - must not be called
            raise AssertionError("inactive host must not receive bridged events")

    thread._extension_host = _Inactive()

    async def scenario():
        a.emit(SubAgentStarted(agent_type="coder", description="d", run_id="r0"))
        await asyncio.sleep(0)

    asyncio.run(scenario())  # 不抛即通过
