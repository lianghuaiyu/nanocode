"""docs/16 #2（STEP D-2）专项：telemetry + injection 事件化——单 emit 出口扇出 [record_event(树), UI 投影]。

断言：
- 遥测 emit 点（LLM_REQUEST / PERMISSION_DECISION / TOOL_BLOCKED / BUDGET_EXCEEDED / TURN_END）
  经 typed 事件 → record_event 后，树 entry 形状与 cutover 前 _tree_event 直写**逐字段一致**；
- TURN_END 每 turn 恰好一条（record_event 单写，无双发），aborted turn 落 finalStatus=cancelled；
- ContextInjected 的写入结果（bool）经 emit 忠实回传（注入器 dedup 依据）；
- ToolBlocked 由 record_event 补 agentType/artifactId 身份（事件本身只携带拦截事实）。
"""

import asyncio

from nanocode.agent.engine import Agent
from nanocode.agent.events import ContextInjected, ToolBlocked
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


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
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def _entries(sid, type_):
    return [e for e in SessionManager.open(sid).entries() if e.type == type_]


# ─── 遥测 emit 点全量重发核对（live turn 驱动）──────────────────────────────────

def test_live_turn_emits_llm_request_permission_and_single_turn_end():
    a = _agent("tel2_live")
    calls = {"n": 0}

    async def fake(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a._provider.stream = fake
    asyncio.run(a.chat("hi"))

    # LLM_REQUEST：每次请求一条，字段形状与原 _tree_event 直写一致。
    reqs = _entries("tel2_live", T.LLM_REQUEST)
    assert len(reqs) == 2
    assert all(set(e.data) >= {"model", "messageCount", "messagesChars"} for e in reqs)
    assert reqs[0].data["model"] == "claude-x"

    # PERMISSION_DECISION：工具派发前的授权决策（bypass → allow）。
    perms = _entries("tel2_live", T.PERMISSION_DECISION)
    assert [(e.data["tool"], e.data["action"]) for e in perms] == [("list_files", "allow")]

    # TURN_END：恰好一条（emit→record_event 单写），累计遥测 + finalStatus。
    te = _entries("tel2_live", T.TURN_END)
    assert len(te) == 1
    assert te[0].data == {"inputTokens": 20, "outputTokens": 10, "turns": 1,
                          "finalStatus": "completed"}


def test_budget_exceeded_entry_via_typed_event():
    a = _agent("tel2_budget")
    a.max_turns = 1

    async def fake(**_kw):
        return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])

    a._provider.stream = fake
    asyncio.run(a.chat("go"))

    bud = _entries("tel2_budget", T.BUDGET_EXCEEDED)
    assert len(bud) == 1 and "Turn limit" in bud[0].data["reason"]


def test_aborted_turn_writes_single_cancelled_turn_end():
    a = _agent("tel2_abort")

    async def fake(**_kw):
        a._aborted = True
        return _FakeResp([_FakeBlock("text", text="partial")])

    a._provider.stream = fake
    asyncio.run(a.chat("hi"))

    te = _entries("tel2_abort", T.TURN_END)
    assert len(te) == 1 and te[0].data["finalStatus"] == "cancelled"


# ─── 单元级：emit 扇出语义 ───────────────────────────────────────────────────

def test_tool_blocked_event_enriched_with_agent_identity():
    a = _agent("tel2_blocked")
    a.agent_type = "coder"
    a.artifact_id = "agent-1"
    a._ensure_session_lease()
    a.emit(ToolBlocked(tool="run_shell", reason="not_in_allowlist"))
    e = _entries("tel2_blocked", T.TOOL_BLOCKED)[0]
    assert e.data == {"tool": "run_shell", "reason": "not_in_allowlist",
                      "agentType": "coder", "artifactId": "agent-1"}


def test_context_injected_emit_returns_write_result_for_dedup():
    a = _agent("tel2_ctx")
    # 无写者租约 → 树写失败 → False（调用方据此**不**推进 dedup）。
    assert a.emit(ContextInjected(custom_type="memory", content="m1")) is False
    a._ensure_session_lease()
    assert a.emit(ContextInjected(custom_type="memory", content="m1")) is True
    cm = _entries("tel2_ctx", T.CUSTOM_MESSAGE)
    assert [e.data["customType"] for e in cm] == ["memory"]
