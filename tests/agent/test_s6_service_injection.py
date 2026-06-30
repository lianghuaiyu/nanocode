"""docs/26 S6 (G1/G5)：内核不自建 ③ 宿主服务 + 注入式 fail-closed + first_attach 保活。

G1（单向依赖）/ G5（服务可换）的根因 = `Agent.__init__` 自建 ③ 具体服务作兜底。S6 后内核
slot 默认 None、deref fail-closed，由 runtime（thread_start/_apply / build_sub_agent）或测试
载体注入；mcp/run/task 首挂保活（跨会话切换不重建），sandbox 每次随 bundle 重建（无状态）。
"""
import pytest

from nanocode.agent.engine import Agent
from nanocode.capabilities.sandbox import SandboxManager
from nanocode.mcp import McpManager
from nanocode.runs.runtime import AgentRunRuntime
from nanocode.runtime.facade import RuntimeServices, _apply_runtime_services
from nanocode.tasks.manager import TaskManager

from .._helpers import build_test_agent

_FAIL_CLOSED = ("_sandbox", "_run_runtime", "_mcp_manager", "task_manager")
_SLOTS = ("_svc_sandbox", "_svc_run_runtime", "_svc_mcp_manager", "_svc_task_manager")


def _full_services(cwd="."):
    return RuntimeServices(
        cwd=cwd, workspace_trusted=True, memory_service=None, context_sources=None,
        sandbox=SandboxManager(), mcp_manager=McpManager(),
        run_runtime=AgentRunRuntime(), task_manager=TaskManager())


# ─── G1：内核构造期不自建 ③ 服务 ──────────────────────────────────────────────

def test_kernel_does_not_self_build_services():
    a = Agent(api_key="test")
    for slot in _SLOTS:
        assert getattr(a, slot) is None, f"{slot} should not be self-built by the kernel"


# ─── G5：未注入即 fail-closed（无声自建兜底消失）──────────────────────────────

def test_deref_uninjected_service_fails_closed():
    a = Agent(api_key="test")
    for attr in _FAIL_CLOSED:
        with pytest.raises(RuntimeError, match="not injected"):
            getattr(a, attr)


def test_injected_services_deref_cleanly():
    a = build_test_agent("s6_inj")
    assert isinstance(a._sandbox, SandboxManager)
    assert isinstance(a._run_runtime, AgentRunRuntime)
    assert isinstance(a._mcp_manager, McpManager)
    assert isinstance(a.task_manager, TaskManager)


def test_setter_routes_to_backing_slot():
    a = Agent(api_key="test")
    sb = SandboxManager()
    a._sandbox = sb                      # 描述符 setter → backing slot
    assert a._svc_sandbox is sb
    assert a._sandbox is sb


# ─── 子 agent 注入：sandbox/task 共享父，run/mcp 各自 fresh（行为保真）────────────

def test_sub_agent_service_injection_shape():
    host = build_test_agent("s6_host")
    sub = host._build_sub_agent(system_prompt="x", tools=[], agent_type="coder")
    assert sub._sandbox is host._sandbox            # 无状态 → 共享
    assert sub.task_manager is host.task_manager    # ctor 参共享父 ledger
    assert sub._run_runtime is not host._run_runtime  # 各自 fresh（子不 spawn 孙）
    assert sub._mcp_manager is not host._mcp_manager  # 各自 inert（子被 MCP 闸挡在外）


# ─── first_attach 保活：rebind 不重建有状态服务（连接/账本/任务状态不丢）────────────

def test_first_attach_preserves_stateful_services_swaps_sandbox():
    a = Agent(api_key="test")
    s1 = _full_services()
    _apply_runtime_services(a, s1)
    mcp1, run1, task1 = a._mcp_manager, a._run_runtime, a.task_manager
    assert (mcp1, run1, task1) == (s1.mcp_manager, s1.run_runtime, s1.task_manager)

    s2 = _full_services()
    _apply_runtime_services(a, s2)        # not first_attach（_runtime_services 已设）
    # mcp/run/task 首挂保活：跨 bundle 重装不换实例
    assert a._mcp_manager is mcp1
    assert a._run_runtime is run1
    assert a.task_manager is task1
    # sandbox 无状态：每次随 bundle 重建
    assert a._sandbox is s2.sandbox
