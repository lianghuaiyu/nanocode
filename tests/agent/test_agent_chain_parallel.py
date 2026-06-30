"""docs/16 #9：agent 工具的 chain / parallel fan-in host 原语。

- chain：按序独立子 agent；{previous} 替换为上一步 bounded envelope；abort 在步边界生效；
- parallel：并发独立子 agent，按任务序聚合；
- 每步/每任务 = 独立 child-session run record（审计可循）；
- 与 resume / run_in_background / 彼此互斥；步数/任务数封顶。
"""

import asyncio
import json

from nanocode.agent.engine import Agent

from .._helpers import attach_orchestration
from .._helpers import inject_test_services


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    a = Agent(api_key="test", session_id="chainsid", **kw)
    inject_test_services(a)
    a._mcp_initialized = True
    attach_orchestration(a)            # steps/tasks 委托到 layer④ orchestration 扩展
    return a


def _stub_build(parent, texts_by_prompt=None, record=None):
    """spy _build_sub_agent：stub run_once 按收到的 prompt 回固定文本并记录调用。"""
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt: str) -> dict:
            if record is not None:
                record.append({"agent_type": kw.get("agent_type"), "prompt": prompt,
                               "artifact_id": kw.get("artifact_id")})
            text = f"RESULT<{prompt.split('|')[0]}>"
            if texts_by_prompt:
                for k, v in texts_by_prompt.items():
                    if k in prompt:
                        text = v
            return {"text": text, "tokens": {"input": 1, "output": 1}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    return record


def test_chain_runs_steps_sequentially_with_previous_substitution():
    parent = _agent()
    record = []
    _stub_build(parent, texts_by_prompt={"step-one": "ONE-OUT"}, record=record)

    out = asyncio.run(parent._execute_agent_tool({
        "description": "chained work",
        "steps": [
            {"type": "explore", "description": "first", "prompt": "step-one|go"},
            {"type": "coder", "description": "second", "prompt": "step-two|use: {previous}"},
        ],
    }))
    assert [r["agent_type"] for r in record] == ["explore", "coder"]   # 顺序执行
    assert "ONE-OUT" in record[1]["prompt"]                            # {previous} 注入上一步 envelope
    assert "{previous}" not in record[1]["prompt"]
    assert "## Step 1/2 [explore] first" in out and "## Step 2/2 [coder] second" in out
    # 每步独立 child-session run record
    recs = json.loads(parent.run_list())
    assert len(recs) == 2
    assert all(r["child_session_id"].startswith("sess_") for r in recs)
    assert {r["artifact_id"] for r in record} == {r["child_session_id"] for r in recs}
    assert all(r["status"] == "completed" for r in recs)


def test_chain_first_step_previous_placeholder_is_neutral():
    parent = _agent()
    record = []
    _stub_build(parent, record=record)
    asyncio.run(parent._execute_agent_tool({
        "description": "d",
        "steps": [{"prompt": "start with {previous}"}],
    }))
    assert "(no previous step)" in record[0]["prompt"]


def test_chain_stops_at_step_boundary_when_aborted():
    parent = _agent()
    record = []
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)

        async def _ro(prompt: str) -> dict:
            record.append(prompt)
            parent._aborted = True            # 第一步后请求中止
            return {"text": "x", "tokens": {"input": 0, "output": 0}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    out = asyncio.run(parent._execute_agent_tool({
        "description": "d",
        "steps": [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}],
    }))
    assert len(record) == 1                    # 后续步不再起跑
    assert "skipped (turn aborted)" in out


def test_parallel_fans_out_and_gathers_in_task_order():
    parent = _agent()
    record = []
    _stub_build(parent, record=record)
    out = asyncio.run(parent._execute_agent_tool({
        "description": "fan out",
        "tasks": [
            {"type": "explore", "description": "ta", "prompt": "alpha|x"},
            {"type": "explore", "description": "tb", "prompt": "beta|y"},
            {"type": "coder", "description": "tc", "prompt": "gamma|z"},
        ],
    }))
    assert len(record) == 3
    i1 = out.index("## Task 1/3"); i2 = out.index("## Task 2/3"); i3 = out.index("## Task 3/3")
    assert i1 < i2 < i3                        # 聚合按任务序（与完成序无关）
    assert "RESULT<alpha" in out and "RESULT<beta" in out and "RESULT<gamma" in out
    recs = json.loads(parent.run_list())
    assert len(recs) == 3 and all(r["status"] == "completed" for r in recs)


def test_steps_tasks_mutual_exclusions():
    parent = _agent()
    _stub_build(parent)
    r = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "steps": [{"prompt": "x"}], "tasks": [{"prompt": "y"}]}))
    assert "mutually exclusive" in r          # 四形状互斥（含 accept/plan_fanout）
    # D6：steps/tasks + run_in_background 不再互斥——派后台编排，立即返回 group id。
    r = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "steps": [{"prompt": "x"}], "run_in_background": True}))
    assert "orch_" in r and "background chain group" in r
    r = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "tasks": [{"prompt": "y"}], "run_in_background": True}))
    assert "orch_" in r and "background parallel group" in r
    # 仍互斥：steps/tasks 与 resume/steer。
    r = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "tasks": [{"prompt": "x"}], "resume": "agent-001"}))
    assert "cannot be combined" in r


def test_orchestration_validation_and_caps():
    parent = _agent()
    r = asyncio.run(parent._execute_agent_tool({"description": "d", "steps": []}))
    assert "non-empty array" in r
    r = asyncio.run(parent._execute_agent_tool(
        {"description": "d", "steps": [{"prompt": ""}]}))
    assert "non-empty 'prompt'" in r
    too_many = [{"prompt": f"p{i}"} for i in range(11)]
    r = asyncio.run(parent._execute_agent_tool({"description": "d", "steps": too_many}))
    assert "too many steps" in r
    r = asyncio.run(parent._execute_agent_tool({"description": "d", "tasks": [{"prompt": "x"}] * 9}))
    assert "too many tasks" in r


def test_single_run_requires_prompt():
    parent = _agent()
    r = asyncio.run(parent._execute_agent_tool({"description": "d"}))
    assert "'prompt' is required" in r
