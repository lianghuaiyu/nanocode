"""docs/26 G7：编排 schema 按编排扩展可用性条件出现。

不变量：常驻 builtin 的 `agent` 工具是 slim BASE_SCHEMA（无 steps/tasks/accept/plan_fanout）；
仅当编排扩展激活时，facade 的 `_apply_runtime_services` 才把 ORCHESTRATION_SCHEMA overlay 进
**主** agent 的 per-agent registry（绝不动全局 REGISTRY、绝不上子 agent）。
"""

from nanocode.agent.engine import Agent
from nanocode.extensions import ExtensionHost
from nanocode.runtime import RuntimeServices
from nanocode.runtime.facade import _apply_runtime_services
from nanocode.tools.agent import BASE_SCHEMA, ORCHESTRATION_SCHEMA
from nanocode.tools.registry import REGISTRY

_ORCH_KEYS = {"steps", "tasks", "accept", "plan_fanout"}


def _agent_schema(agent) -> dict:
    return agent.registry.get("agent").schema


def _orch_keys(schema: dict) -> set:
    return _ORCH_KEYS & set(schema["input_schema"]["properties"])


def _services(host) -> RuntimeServices:
    return RuntimeServices(
        cwd=".", workspace_trusted=True, memory_service=None,
        context_sources=None, extension_host=host)


def test_fresh_main_agent_has_slim_agent_schema():
    # 未经 runtime 装配的主 agent：registry 里 `agent` 就是常驻 slim BASE_SCHEMA。
    a = Agent(api_key="test")
    assert _orch_keys(_agent_schema(a)) == set()


def test_no_orchestration_extension_keeps_slim():
    # 有 host 但无编排扩展（空 host）→ _apply 不注入编排 schema。
    a = Agent(api_key="test")
    host = ExtensionHost([]).activate_all()
    assert host.registry.orchestrator is None
    _apply_runtime_services(a, _services(host))
    assert _orch_keys(_agent_schema(a)) == set()


def test_orchestration_extension_unlocks_full_schema():
    # 系统扩展（含 orchestration）激活后 _apply → 主 agent 的 `agent` schema 变 ORCHESTRATION 面。
    a = Agent(api_key="test")
    host = ExtensionHost.load_system_extensions().activate_all()
    assert host.registry.orchestrator is not None
    _apply_runtime_services(a, _services(host))
    schema = _agent_schema(a)
    assert _orch_keys(schema) == _ORCH_KEYS
    # description 也带上编排词汇，且 schema 仍是闭合的（additionalProperties=false，与 builtins 一致）。
    assert "{previous}" in schema["description"]
    assert schema["input_schema"]["additionalProperties"] is False
    # agent.tools（发给 API 的 schema 列表）也反映了升级后的面。
    agent_tool = next(t for t in a.tools if t["name"] == "agent")
    assert _orch_keys(agent_tool) == _ORCH_KEYS


def test_sub_agent_never_gets_orchestration_schema():
    # 子 agent 不走该 overlay（编排是主 agent 面）：即便 host 有编排扩展，子 agent 保持 slim。
    a = Agent(api_key="test", is_sub_agent=True)
    host = ExtensionHost.load_system_extensions().activate_all()
    _apply_runtime_services(a, _services(host))
    assert _orch_keys(_agent_schema(a)) == set()


def test_overlay_set_schema_does_not_mutate_global_registry():
    # set_schema 只改 per-agent overlay；全局 REGISTRY 的 `agent` 仍 slim。
    a = Agent(api_key="test")
    host = ExtensionHost.load_system_extensions().activate_all()
    _apply_runtime_services(a, _services(host))
    assert _orch_keys(_agent_schema(a)) == _ORCH_KEYS
    assert _orch_keys(REGISTRY.get("agent").schema) == set()
    # 模块级字面量未被原地改动。
    assert _ORCH_KEYS & set(BASE_SCHEMA["input_schema"]["properties"]) == set()
    assert _ORCH_KEYS <= set(ORCHESTRATION_SCHEMA["input_schema"]["properties"])


def test_apply_is_idempotent_across_rebind():
    # rebind 复用同一 _registry：重复 _apply 不报错、schema 稳定为 ORCHESTRATION 面。
    a = Agent(api_key="test")
    host = ExtensionHost.load_system_extensions().activate_all()
    svc = _services(host)
    _apply_runtime_services(a, svc)
    _apply_runtime_services(a, svc)
    assert _orch_keys(_agent_schema(a)) == _ORCH_KEYS
