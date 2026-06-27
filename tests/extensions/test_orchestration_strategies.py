"""docs/26 §0.6 策略库：acceptance-gate + plan-then-fanout 端到端（经 agent 工具 → layer④扩展）。

stub `_build_sub_agent.run_once` 按 agent_type 回 canned worker/reviewer/planner 输出;经
`_agent_with_orchestration` 接上 bound orchestration 扩展宿主，走完整 execute_agent_tool 路径。
"""
import asyncio
import json

from nanocode.agent.engine import Agent

from .._helpers import attach_orchestration


def _agent():
    a = Agent(api_key="test", session_id="orchstrat", permission_mode="bypassPermissions")
    a._mcp_initialized = True
    attach_orchestration(a)
    return a


def _spy(parent, responder):
    """responder(agent_type, prompt) -> text。覆写 run_once 回 canned + 写 child 树消息。"""
    real = parent._build_sub_agent

    def _b(**kw):
        sub = real(**kw)

        async def _ro(prompt: str) -> dict:
            text = responder(kw.get("agent_type"), prompt)
            if sub._session_mgr is not None:
                sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
                sub.agent_session.record_provider_messages({"role": "assistant", "content": text})
            return {"text": text, "tokens": {"input": 1, "output": 1}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _b


# ─── acceptance-gate ────────────────────────────────────────────────────────────

def test_accept_reviewer_accepts_first_round():
    parent = _agent()
    _spy(parent, lambda at, p: '{"accept": true, "feedback": ""}' if at == "explore" else "worker output v1")
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"type": "coder", "prompt": "do X"},
                   "reviewer": {"type": "explore", "prompt": "review {output}"}}}))
    assert "ACCEPTED" in out and "Round 1/3 — ACCEPTED" in out
    assert "worker output v1" in out


def test_accept_retries_with_feedback_then_accepts():
    parent = _agent()
    seen, state = [], {"rv": 0}

    def responder(at, prompt):
        if at == "explore":                       # reviewer
            state["rv"] += 1
            return ('{"accept": false, "feedback": "needs more detail"}' if state["rv"] == 1
                    else '{"accept": true, "feedback": "good"}')
        seen.append(prompt)                       # worker
        return f"worker output (after {state['rv']} reviews)"

    _spy(parent, responder)
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"prompt": "do X"}, "reviewer": {"prompt": "review {output}"},
                   "max_rounds": 3}}))
    assert "Round 1/3 — rejected" in out and "Round 2/3 — ACCEPTED" in out
    assert "needs more detail" in out                          # 反馈出现在结果
    assert any("needs more detail" in p for p in seen)         # 反馈串接进 worker 第二轮 prompt


def test_accept_exhausts_max_rounds():
    parent = _agent()
    _spy(parent, lambda at, p: '{"accept": false, "feedback": "nope"}' if at == "explore" else "bad")
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"prompt": "x"}, "reviewer": {"prompt": "{output}"}, "max_rounds": 2}}))
    assert "NOT accepted after 2 round(s)" in out
    assert out.count("rejected") == 2


def test_accept_output_schema_pass():
    parent = _agent()
    _spy(parent, lambda at, p: '{"name": "x", "count": 3}')
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"prompt": "emit json"},
                   "output_schema": {"type": "object", "required": ["name", "count"],
                                     "properties": {"name": {"type": "string"},
                                                    "count": {"type": "integer"}}}}}))
    assert "ACCEPTED" in out


def test_accept_output_schema_fail_feeds_error():
    parent = _agent()
    _spy(parent, lambda at, p: '{"name": "x"}')                # 缺 count
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"prompt": "emit json"}, "max_rounds": 1,
                   "output_schema": {"type": "object", "required": ["count"]}}}))
    assert "NOT accepted" in out
    assert "missing required key 'count'" in out


def test_accept_schema_and_reviewer_both_gate():
    parent = _agent()
    _spy(parent, lambda at, p: '{"accept": true}' if at == "explore" else '{"ok": true}')
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"prompt": "x"}, "reviewer": {"type": "explore", "prompt": "{output}"},
                   "output_schema": {"type": "object", "required": ["ok"]}}}))
    assert "ACCEPTED" in out


def test_accept_validation_errors():
    parent = _agent()
    _spy(parent, lambda at, p: "x")
    assert "accept.worker" in asyncio.run(parent._execute_agent_tool({"accept": {}}))
    assert "at least one verifier" in asyncio.run(
        parent._execute_agent_tool({"accept": {"worker": {"prompt": "x"}}}))


def test_accept_run_in_background_rejected():
    parent = _agent()
    _spy(parent, lambda at, p: "x")
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"prompt": "x"}, "output_schema": {"type": "object"}},
        "run_in_background": True}))
    assert "foreground only" in out


# ─── plan-then-fanout ────────────────────────────────────────────────────────────

def test_plan_fanout_decomposes_and_aggregates():
    parent = _agent()

    def responder(at, prompt):
        if at == "plan":
            return ('[{"description":"a","prompt":"do a"},'
                    '{"description":"b","prompt":"do b","type":"explore"},'
                    '{"prompt":"do c"}]')
        return f"result:{prompt[:8]}"

    _spy(parent, responder)
    out = asyncio.run(parent._execute_agent_tool({
        "plan_fanout": {"planner": {"type": "plan", "prompt": "decompose"}, "worker_type": "coder"}}))
    assert "decomposed into 3 worker(s)" in out and "## Plan" in out
    assert "## Worker 1/3" in out and "## Worker 3/3" in out


def test_plan_fanout_caps_to_max_workers():
    parent = _agent()
    workers = []

    def responder(at, prompt):
        if at == "plan":
            return json.dumps([{"prompt": f"t{i}"} for i in range(5)])
        workers.append(prompt)
        return "r"

    _spy(parent, responder)
    out = asyncio.run(parent._execute_agent_tool({
        "plan_fanout": {"planner": {"type": "plan", "prompt": "d"}, "max_workers": 2}}))
    assert "decomposed into 2 worker(s)" in out
    assert len(workers) == 2


def test_plan_fanout_bad_planner_json():
    parent = _agent()
    _spy(parent, lambda at, p: "no json here" if at == "plan" else "r")
    out = asyncio.run(parent._execute_agent_tool({
        "plan_fanout": {"planner": {"type": "plan", "prompt": "d"}}}))
    assert out.startswith("Error") and "valid JSON" in out


# ─── 互斥 ────────────────────────────────────────────────────────────────────────

def test_orchestration_shapes_mutually_exclusive():
    parent = _agent()
    _spy(parent, lambda at, p: "x")
    out = asyncio.run(parent._execute_agent_tool({
        "accept": {"worker": {"prompt": "x"}}, "tasks": [{"prompt": "y"}]}))
    assert "mutually exclusive" in out
    out = asyncio.run(parent._execute_agent_tool({
        "plan_fanout": {"planner": {"prompt": "x"}}, "steps": [{"prompt": "y"}]}))
    assert "mutually exclusive" in out
