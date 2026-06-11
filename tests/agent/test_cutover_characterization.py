"""S0 cutover characterization：钉住当前注入行为 + live 请求来源，作为重家注入的安全网。

注入函数（_inject_*）无既有单测；cutover 要把它们重家为 render-time 装饰。这些测试 pin 其契约，
重家后必须仍绿。另含 request-capture e2e（stub backend stream），S2 redirect 后复用以验证请求改自树。
"""

import asyncio
import copy

from nanocode.agent.engine import Agent
from nanocode.skills.listing import append_to_last_user


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="char", **kw)


# ─── append_to_last_user：3 个注入共用/重实现的 helper（最高价值 pin） ──────────
def test_append_to_last_user_str_concat():
    msgs = [{"role": "user", "content": "hi"}]
    append_to_last_user(msgs, "EXTRA")
    assert msgs[-1]["content"] == "hi\n\nEXTRA"


def test_append_to_last_user_list_appends_text_block():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    append_to_last_user(msgs, "EXTRA")
    assert msgs[-1]["content"][-1] == {"type": "text", "text": "EXTRA"}


def test_append_to_last_user_non_user_last_appends_new_message():
    msgs = [{"role": "assistant", "content": [{"type": "text", "text": "a"}]}]
    append_to_last_user(msgs, "EXTRA")
    assert msgs[-1] == {"role": "user", "content": "EXTRA"}


def test_append_to_last_user_empty_appends_new():
    msgs = []
    append_to_last_user(msgs, "EXTRA")
    assert msgs == [{"role": "user", "content": "EXTRA"}]


# ─── _inject_pending_skill_bodies：append 整条 + 抽干队列 ──────────────────────
def test_inject_pending_skill_bodies_appends_and_drains():
    a = _agent()
    a._pending_skill_bodies = [("skillX", "BODYTEXT")]
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_pending_skill_bodies(msgs)
    assert any("BODYTEXT" in str(m.get("content", "")) for m in msgs[1:])
    assert a._pending_skill_bodies == []


# ─── _inject_skill_listing：append_to_last_user + 更新 _sent_skill_names ───────
def test_inject_skill_listing_appends_and_updates_sent(monkeypatch):
    a = _agent()
    monkeypatch.setattr("nanocode.agent.engine.skill_listing_delta",
                        lambda sent, activated, budget: ("LISTING-X", {"s1"}))
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_skill_listing(msgs)
    assert "LISTING-X" in msgs[-1]["content"]
    assert "s1" in a._sent_skill_names


def test_inject_skill_listing_noop_for_subagent(monkeypatch):
    a = _agent()
    a.is_sub_agent = True
    monkeypatch.setattr("nanocode.agent.engine.skill_listing_delta",
                        lambda *a, **k: ("SHOULD-NOT-APPEAR", {"s1"}))
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_skill_listing(msgs)
    assert msgs == [{"role": "user", "content": "hi"}]


# ─── _inject_finished_tasks：mutate last user + mark injected ──────────────────
def test_inject_finished_tasks_mutates_last_user_and_marks(monkeypatch):
    a = _agent()

    class _T:
        id = "task1"

    monkeypatch.setattr("nanocode.agent.engine.collect_pending_injections", lambda tm: [_T()])
    monkeypatch.setattr("nanocode.agent.engine.render_task_reminder", lambda t: "REMINDER")
    marked = []
    monkeypatch.setattr(a.task_manager, "update_task",
                        lambda tid, **kw: marked.append((tid, kw)), raising=False)
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_finished_tasks(msgs)
    assert "REMINDER" in msgs[-1]["content"]
    assert marked == [("task1", {"injected": True})]


# ─── e2e request-capture 基线（S2 redirect 后复用） ───────────────────────────
class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResp:
    def __init__(self):
        self.content = [_FakeBlock("ok")]
        self.usage = _FakeUsage()


def test_live_request_built_from_message_list_baseline():
    a = _agent()
    a._mcp_initialized = True  # 跳过 MCP 连接
    captured = {}

    async def fake_stream(**_kw):
        captured["messages"] = copy.deepcopy(a._anthropic_messages)
        return _FakeResp()

    a._provider.stream = fake_stream
    asyncio.run(a.chat("hello-baseline"))
    msgs = captured["messages"]
    assert any("hello-baseline" in str(m.get("content", "")) for m in msgs)  # user msg 进入请求
