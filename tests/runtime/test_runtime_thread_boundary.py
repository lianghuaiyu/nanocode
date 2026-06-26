"""docs/23 Phase 0：RuntimeThread 外部边界——不得公开 raw `.agent` / `.session`。

外部控制面只能拿稳定 facade（docs/23 §4.3）；raw Agent/AgentSession 句柄是 runtime/
session 内部实现，绝不跨 CLI/RPC/TUI/SDK 边界。本测试按 §9 推荐方式经 AgentRuntime 构造
一个真实 RuntimeThread，断言实例上无 public `.agent` / `.session`。
"""

from nanocode.runtime import AgentConfig, AgentRuntime, RuntimeThread


def _thread() -> RuntimeThread:
    rt = AgentRuntime()
    return rt.thread_start(AgentConfig(api_key="test", session_id="boundary",
                                       permission_mode="bypassPermissions"))


def test_runtime_thread_instance_has_no_public_agent_or_session():
    thread = _thread()
    try:
        assert isinstance(thread, RuntimeThread)
        assert not hasattr(thread, "agent")
        assert not hasattr(thread, "session")
    finally:
        thread.release_lease()


def test_runtime_thread_exposes_attach_approvals_replacement_surface():
    # docs/23 §4.3：审批接线经 facade，而不是外部直 attach raw agent。
    thread = _thread()
    try:
        assert hasattr(thread, "attach_approvals")
    finally:
        thread.release_lease()
