"""A1 (docs/25 §4)：reserved-agent spawn 走 child-session run_record（无 TaskManager 幽灵调用）。

修复前：`run_reserved_agent` 第一句即调 `host.task_manager.create_subagent(...)`——
`TaskManager` 没有该方法 → `AttributeError`，被 `memory_evolution` 的诊断桥吞成 `[]`，
检索诊断子 agent 从不真正工作。修复后：走 `begin_run_record`/`finish_run_record` 四态对称，
不再触 `TaskManager`。
"""
import asyncio

from nanocode.agent.engine import Agent
from nanocode.runs.models import TERMINAL_RUN_STATUSES
from nanocode.subagents import run_record
from nanocode.subagents.prompts import MEMORY_RETRIEVAL_DIAGNOSIS_TYPE


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="reserved_parent", **kw)


def _spy_build_with_stub(parent, *, text):
    """spy `_build_sub_agent`：注入 stub `run_once`（写 child 树 + 返回固定 text）。返回 built dict。"""
    built = {}
    real_build = parent._build_sub_agent

    def _spy(**kw):
        sub = real_build(**kw)
        built["sub"] = sub

        async def _ro(prompt: str) -> dict:
            if sub._session_mgr is not None:        # 真 run_once 会写 child 树——stub 对齐
                sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
                sub.agent_session.record_provider_messages({"role": "assistant", "content": text})
            return {"text": text, "tokens": {"input": 11, "output": 7}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy
    return built


def test_reserved_agent_no_attributeerror_and_terminal_run_record():
    """修复前此调用即 AttributeError；修复后返回文本并落终态 run_record + child session.jsonl。"""
    parent = _agent()
    stub_text = '{"parameter_suggestions": {"semantic_top_k": 35}}'
    built = _spy_build_with_stub(parent, text=stub_text)

    async def scenario():
        out = await parent._run_reserved_agent(
            agent_type=MEMORY_RETRIEVAL_DIAGNOSIS_TYPE, prompt="diagnose pls")
        child_id = built["sub"]._tree_session_id
        return out, child_id, run_record.read_status(child_id)

    out, child_id, status = asyncio.run(scenario())

    assert out == stub_text
    assert status["status"] == "completed"
    assert status["status"] in TERMINAL_RUN_STATUSES
    # reserved-agent 不再镜像到 TaskManager（单账本，A2 前置约束）。
    assert parent.task_manager.get_task(child_id) is None


def test_reserved_agent_text_feeds_diagnosis_parse():
    """诊断桥 `_parse_suggestions` 能从 reserved-agent 文本解析出非空建议（修复前恒 `[]`）。"""
    from nanocode.extensions.memory_evolution.agents import _parse_suggestions

    parent = _agent()
    stub_text = ('{"root_causes": ["low recall"], '
                 '"parameter_suggestions": {"semantic_top_k": 35}, "risk": "low"}')
    _spy_build_with_stub(parent, text=stub_text)

    out = asyncio.run(parent._run_reserved_agent(
        agent_type=MEMORY_RETRIEVAL_DIAGNOSIS_TYPE, prompt="diagnose"))

    assert _parse_suggestions(out) == [{"semantic_top_k": 35}]
