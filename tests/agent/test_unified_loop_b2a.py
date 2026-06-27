"""docs/16 B2-a：两 turn 循环合一为 provider-agnostic 的 post-stream 单循环（Pi 风格）。

钉住合一后的两条不变量,补 test_event_inversion_paths 之外的覆盖：
1) **tree/usage 等价**：同一逻辑回合喂给 anthropic vs openai 适配器,写出的 canonical 树
   （MESSAGE 序列 + stopReason/usage/toolResult 内容）除 provider wire 形状外语义一致；
2) **Anthropic 并发安全工具 post-stream 仍并行**：连续 CONCURRENCY_SAFE 工具经 batch 模型
   asyncio.gather 并行执行（去掉的只是流中预启动,不是并行本身）——否则 = 真串行回归。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager

from .._helpers import attach_runtime_agent


class _FakeBlock:
    def __init__(self, type="text", **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 11
    output_tokens = 7


class _FakeResp:
    def __init__(self, content, stop_reason=None):
        self.content = content
        self.usage = _FakeUsage()
        if stop_reason is not None:
            self.stop_reason = stop_reason


def _agent(sid, **kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    a = Agent(api_key="test", session_id=sid, **kw)
    a._mcp_initialized = True
    if a.provider_runtime_config.name != "openai":
        a.model = "claude-x"
    attach_runtime_agent(a)   # docs/23 Phase 4: inject session-writer lease (was a.chat() pre-cutover)
    return a


def _openai_agent(sid):
    return _agent(sid, api_base="http://localhost:1/v1", model="gpt-test")


def _msgs(sid):
    return [e.data["message"] for e in SessionManager.open(sid).entries() if e.type == T.MESSAGE]


# ── 1) tree/usage 等价（两 provider 跑同一逻辑回合）────────────────────────────

def test_unified_loop_anthropic_tree_usage():
    a = _agent("b2a_anth")

    async def fake_stream(**_kw):
        if not hasattr(fake_stream, "done"):
            fake_stream.done = True
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})],
                             stop_reason="tool_use")
        return _FakeResp([_FakeBlock("text", text="done")], stop_reason="end_turn")

    a._provider.stream = fake_stream
    asyncio.run(a._chat_internal("go"))

    m = _msgs("b2a_anth")
    assert [x["role"] for x in m] == ["user", "assistant", "toolResult", "assistant"]
    assert m[1]["stopReason"] == "toolUse" and m[3]["stopReason"] == "stop"
    assert m[1]["usage"] == {"inputTokens": 11, "outputTokens": 7}   # Completion.usage 落树
    assert m[2]["toolCallId"] == "t1" and isinstance(m[2].get("latencyMs"), int)


def test_unified_loop_openai_tree_usage():
    a = _openai_agent("b2a_oai")
    calls = {"n": 0}

    async def fake_stream(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    "choices": [{"finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None, "tool_calls": [
                            {"type": "function", "id": "t1",
                             "function": {"name": "list_files", "arguments": '{"path": "."}'}}]}}]}
        return {"usage": {"prompt_tokens": 3, "completion_tokens": 1},
                "choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": "done"}}]}

    a._provider.stream = fake_stream
    asyncio.run(a._chat_internal("go"))

    m = _msgs("b2a_oai")
    # 同一逻辑回合：相同 role 序列 + 相同 stopReason/usage 语义（toolResult wire 形状 provider 各异但树语义一致）
    assert [x["role"] for x in m] == ["user", "assistant", "toolResult", "assistant"]
    assert m[1]["stopReason"] == "toolUse" and m[3]["stopReason"] == "stop"
    assert m[1]["usage"] == {"inputTokens": 11, "outputTokens": 7}
    assert m[2]["toolCallId"] == "t1" and isinstance(m[2].get("latencyMs"), int)


# ── 2) Anthropic 并发安全工具 post-stream 仍并行（batch gather）────────────────

def test_anthropic_concurrent_safe_tools_run_in_parallel_post_stream():
    a = _agent("b2a_parallel")
    calls = {"n": 0}
    inflight = {"cur": 0, "max": 0}

    async def slow_exec(name, inp):
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        await asyncio.sleep(0.05)            # 串行执行 → max=1；并行(gather) → max=2
        inflight["cur"] -= 1
        return "ok"

    a._execute_tool_call = slow_exec

    async def fake_stream(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # 两个连续 CONCURRENCY_SAFE（list_files）→ 同一并行 batch
            return _FakeResp([
                _FakeBlock("tool_use", id="t1", name="list_files", input={"path": "a"}),
                _FakeBlock("tool_use", id="t2", name="list_files", input={"path": "b"}),
            ], stop_reason="tool_use")
        return _FakeResp([_FakeBlock("text", text="done")], stop_reason="end_turn")

    a._provider.stream = fake_stream
    asyncio.run(a._chat_internal("scan"))

    assert inflight["max"] == 2              # 并行执行（gather）——非串行回归
    m = _msgs("b2a_parallel")
    # Anthropic：两条 tool_result 合成一条批量 user 消息
    assert [x["role"] for x in m] == ["user", "assistant", "toolResult", "toolResult", "assistant"]
    assert {m[2]["toolCallId"], m[3]["toolCallId"]} == {"t1", "t2"}


def test_anthropic_unsafe_tools_run_serially_post_stream():
    a = _agent("b2a_serial")
    calls = {"n": 0}
    inflight = {"cur": 0, "max": 0}

    async def slow_exec(name, inp):
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        await asyncio.sleep(0.02)
        inflight["cur"] -= 1
        return "ok"

    a._execute_tool_call = slow_exec

    async def fake_stream(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # run_shell 非并发安全 → 各自串行 batch
            return _FakeResp([
                _FakeBlock("tool_use", id="t1", name="run_shell", input={"command": "echo a"}),
                _FakeBlock("tool_use", id="t2", name="run_shell", input={"command": "echo b"}),
            ], stop_reason="tool_use")
        return _FakeResp([_FakeBlock("text", text="done")], stop_reason="end_turn")

    a._provider.stream = fake_stream
    asyncio.run(a._chat_internal("run"))

    assert inflight["max"] == 1              # 串行执行（不安全工具不并行）
