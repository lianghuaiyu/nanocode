"""docs/15 Phase 7 §6：RuntimeThread.events() —— in-process 事件流订阅(SDK/协议消费者)。"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.runtime import AgentRuntime, RuntimeThread
from nanocode.agent.session import AgentSession
from nanocode.agent.sink import NullSink, RecordingSink


class _FakeBlock:
    def __init__(self, type="text", **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = _FakeUsage()


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions", sink=NullSink())
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def test_recording_sink_standalone():
    s = RecordingSink()
    s.tool_call("read_file", {"file_path": "/x"})
    s.cost(1, 2)
    assert s.records == [
        ("tool_call", {"name": "read_file", "input": {"file_path": "/x"}}),
        ("cost", {"input_tokens": 1, "output_tokens": 2}),
    ]
    s.reset()
    assert s.records == []


def test_recording_sink_pushes_to_queue():
    q = asyncio.Queue()
    s = RecordingSink(queue=q)
    s.info("hi")
    assert q.get_nowait() == ("info", {"message": "hi"})


def test_runtime_thread_events_records_turn_stream():
    a = _agent("evt1")
    calls = {"n": 0}

    async def fake(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a._provider.stream = fake
    thread = AgentRuntime().adopt(a)
    asyncio.run(thread.run("hello"))

    kinds = [k for k, _ in thread.events()]
    # fake provider 不调用流式 text_block 回调（那是 adapter 真流式才有）→ 无 assistant_markdown;
    # 但 tool 派发 + 结束 cost 经 sink 投影被记录,证明 events() 捕获了 thread 的事件流。
    assert "tool_call" in kinds and "tool_result" in kinds
    assert "cost" in kinds
    tc = next(f for k, f in thread.events() if k == "tool_call")
    assert tc["name"] == "list_files"
    tr = next(f for k, f in thread.events() if k == "tool_result")
    assert tr["name"] == "list_files"


def test_events_empty_without_recorder():
    a = _agent("evt2")
    t = RuntimeThread(AgentRuntime(), a, AgentSession(a))      # 直接构造,无 recorder
    assert t.events() == []
