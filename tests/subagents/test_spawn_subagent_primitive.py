"""docs/26 阶段1 ①：内核 spawn_subagent 原语 + 非提权派生。

- spawn_subagent 返回结构化 SubagentOutcome（run_id/status/text/tokens/result_path）；
- 子工具集由内核 child_tools→effective_child_tools 派生（allow∩/deny∪/剔 agent），
  扩展/编排消费方**不能**给子裸配工具（docs/26 §0.3 O5 命门）。
"""
import asyncio
import inspect

from nanocode.agent.engine import Agent
from nanocode.agents.permissions import effective_child_tools
from nanocode.agents.registry import build_profile
from nanocode.runs.models import TERMINAL_RUN_STATUSES
from nanocode.runtime.spawn import SubagentOutcome, live_agent_profile
from nanocode.subagents import run_record
from nanocode.tools import REGISTRY


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", session_id="spawn_parent", **kw)


def _spy_build(parent, *, text, capture):
    """spy `_build_sub_agent`：注入 stub run_once + 捕获传入的 tools/built sub。"""
    real_build = parent._build_sub_agent

    def _spy(**kw):
        capture["tools"] = [t["name"] for t in kw.get("tools", [])]
        sub = real_build(**kw)
        capture["sub"] = sub

        async def _ro(prompt: str) -> dict:
            if sub._session_mgr is not None:
                sub.agent_session.record_provider_messages({"role": "user", "content": prompt})
                sub.agent_session.record_provider_messages({"role": "assistant", "content": text})
            return {"text": text, "tokens": {"input": 11, "output": 7}}

        sub.run_once = _ro
        return sub

    parent._build_sub_agent = _spy


def test_spawn_subagent_returns_structured_outcome():
    parent = _agent()
    cap = {}
    _spy_build(parent, text="hello world", capture=cap)

    async def scenario():
        outcome = await parent._spawn_subagent(profile=build_profile("coder"),
                                               prompt="do a thing")
        child_id = cap["sub"]._tree_session_id
        return outcome, child_id, run_record.read_status(child_id)

    outcome, child_id, status = asyncio.run(scenario())

    assert isinstance(outcome, SubagentOutcome)
    assert outcome.status == "completed" and outcome.status in TERMINAL_RUN_STATUSES
    assert outcome.run_id == child_id
    assert outcome.text == "hello world"
    assert outcome.tokens == {"input": 11, "output": 7}
    assert outcome.result_path  # finish_run_record 写了 result.md 指针
    assert status["status"] == "completed"


def test_spawn_subagent_child_tools_are_kernel_derived_no_escalation():
    parent = _agent()
    cap = {}
    _spy_build(parent, text="{}", capture=cap)

    asyncio.run(parent._spawn_subagent(profile=build_profile("coder"), prompt="p"))

    profile = build_profile("coder")
    universe = {t["name"] for t in REGISTRY.schemas()}
    expected = effective_child_tools(live_agent_profile(parent), profile, universe,
                                     background=True)
    # 子工具 = 内核派生集（逐项相等），且永不含 'agent'（子不 spawn 孙）。
    assert set(cap["tools"]) == expected
    assert "agent" not in cap["tools"]
    assert set(cap["tools"]) <= universe


def test_spawn_subagent_signature_takes_profile_not_raw_tools():
    # 结构性非提权保证：原语签名收 profile，绝无 tools/sandbox 入参。
    params = inspect.signature(Agent._spawn_subagent).parameters
    # _spawn_subagent 经 **kw 转发；核对底层 runner 原语签名。
    from nanocode.runtime.spawn import SubAgentRunner
    rparams = inspect.signature(SubAgentRunner.spawn_subagent).parameters
    assert "profile" in rparams
    assert "tools" not in rparams and "sandbox" not in rparams and "sandbox_profile" not in rparams
