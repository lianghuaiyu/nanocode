"""docs/14 Milestone B (B1)：canonical 树承载原本只进 wire 的派生遥测——但这些遥测对 LLM **不可见**：
不在 FOLD_TYPES（不进 build_context）、不推进 leaf（注解非对话）、消息上的 usage/latencyMs 被 render 丢弃。
并守护三层边界：写树绝不带 reward/eval_result。
"""

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager
from nanocode.session.render import ModelCtx, render


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


TELEMETRY_TYPES = [T.PERMISSION_DECISION, T.TOOL_BLOCKED, T.BUDGET_EXCEEDED,
                   T.TURN_END, T.SESSION_END, T.LLM_REQUEST]


def test_telemetry_types_not_folded():
    for t in TELEMETRY_TYPES:
        assert t not in T.FOLD_TYPES


def test_telemetry_entries_do_not_advance_leaf_or_enter_context():
    m = SessionManager.create("teltree")
    u = m.append_message(T.user_message("hi"))
    a = m.append_message(T.assistant_message([T.text_block("ok")], provider="anthropic",
                         api="anthropic", model="cx", stop_reason="stop"))
    # 追加各类遥测 entry（注解）——leaf 不动、context 仍只有 user+assistant
    for t in TELEMETRY_TYPES:
        m.append(t, {"telmarker": "ZZTELZZ"})
    assert m.get_leaf() == a.id                              # 遥测不推进 leaf（仍在 assistant 上）
    built = m.build_context()
    roles = [msg["role"] for msg in built.messages]
    assert roles == ["user", "assistant"]                    # 遥测不进 LLM 上下文
    assert "ZZTELZZ" not in str(built.messages)


def test_usage_and_latency_stored_but_dropped_by_render():
    m = SessionManager.create("teltel")
    m.append_message(T.user_message("q"))
    m.append_message(T.assistant_message([T.text_block("a")], provider="anthropic", api="anthropic",
                     model="cx", stop_reason="stop", usage={"inputTokens": 10, "outputTokens": 5},
                     latency_ms=1234))
    # 树里存着 usage/latencyMs（trajectory 派生用）
    asst = [e for e in m.entries() if e.type == T.MESSAGE
            and e.data["message"]["role"] == "assistant"][0].data["message"]
    assert asst["usage"] == {"inputTokens": 10, "outputTokens": 5} and asst["latencyMs"] == 1234
    # 但 render 出的 provider 消息**不含** usage/latencyMs（对 LLM 不可见）
    built = m.build_context()
    msgs = render(built.messages, ModelCtx(provider="anthropic", api="anthropic", model_id="cx"))["messages"]
    flat = str(msgs)
    assert "latencyMs" not in flat and "inputTokens" not in flat and "usage" not in flat


def test_tool_result_latency_dropped_by_render():
    m = SessionManager.create("teltr")
    m.append_message(T.user_message("q"))
    m.append_message(T.assistant_message([T.tool_call_block("t1", "read_file", {"p": "x"})],
                     provider="anthropic", api="anthropic", model="cx", stop_reason="toolUse"))
    m.append_message(T.tool_result_message(tool_call_id="t1", tool_name="read_file",
                     content="DATA", latency_ms=42))
    tr = [e for e in m.entries() if e.type == T.MESSAGE
          and e.data["message"]["role"] == "toolResult"][0].data["message"]
    assert tr["latencyMs"] == 42
    built = m.build_context()
    msgs = render(built.messages, ModelCtx(provider="anthropic", api="anthropic", model_id="cx"))["messages"]
    assert "latencyMs" not in str(msgs)


def test_tree_event_strips_reward_and_eval_result():
    # 三层边界：派生标签绝不进事实源（_tree_event 防御性剥除，取代原 Tracer.emit 的同名剥除）。
    a = _agent("telbound")
    a._session_mgr = SessionManager.create("telbound")
    a._tree_event(T.PERMISSION_DECISION, tool="run_shell", action="deny",
                  reward=-1.0, eval_result={"signal": "x"})
    pd = [e for e in a._session_mgr.entries() if e.type == T.PERMISSION_DECISION][0]
    assert "reward" not in pd.data and "eval_result" not in pd.data
    assert pd.data["tool"] == "run_shell" and pd.data["action"] == "deny"
