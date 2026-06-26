"""docs/15 Phase 5：capabilities 层 —— 不可变 PermissionContext + dispatch taxonomy。

验收：PermissionContext 与 PermissionEngine duck-type 契约一致（无需 Agent 即可决策）;
单一 allowlist 咽喉点保持 fail-closed（'agent' 永拦、meta 放行、真实工具按集判定）。
"""

from nanocode.agents.profile import AgentProfile, PermissionProfile
from nanocode.capabilities import (
    Capability, PermissionContext, classify_capability, decide,
    is_always_allowed_meta, router_allowlist_blocks,
)
from nanocode.capabilities.router import CapabilityRouter


# ─── PermissionContext（不可变,decouple from Agent）──────────────────────────
def test_permission_context_duck_types_engine():
    ctx = PermissionContext(mode="bypassPermissions")
    d = decide(ctx, "run_shell", {"command": "rm -rf /"})
    assert d.action == "allow"                      # bypass：放行（与 Agent 路径一致）


def test_permission_context_plan_mode_blocks_edits():
    ctx = PermissionContext(mode="plan", plan_file_path="/tmp/plan.md")
    assert decide(ctx, "write_file", {"file_path": "/tmp/other.py"}).action == "deny"
    assert decide(ctx, "write_file", {"file_path": "/tmp/plan.md"}).action == "allow"   # 计划文件可写
    assert decide(ctx, "read_file", {"file_path": "/x"}).action == "allow"              # 只读放行


def test_permission_context_allowlist_blocks_marks_decision():
    ctx = PermissionContext(mode="bypassPermissions", allowed_tool_names=frozenset({"read_file"}))
    d = decide(ctx, "run_shell", {"command": "ls"})
    assert d.allowlist_blocked is True              # run_shell 不在子 agent 有效集
    d2 = decide(ctx, "read_file", {"file_path": "/x"})
    assert d2.allowlist_blocked is False


def test_from_profile_builds_context():
    prof = AgentProfile(name="explore", permission=PermissionProfile(mode="default"))
    ctx = PermissionContext.from_profile(prof, effective_tool_names={"read_file", "grep_search"})
    assert ctx.mode == "default"
    assert ctx.allowed_tool_names == frozenset({"read_file", "grep_search"})
    assert router_allowlist_blocks("run_shell", ctx.allowed_tool_names) is True


def test_decide_noninteractive_confirm_becomes_deny():
    # docs/19 review：embedded 基底层——非交互上下文下 confirm 收敛为 deny（fail-closed）。
    ctx_i = PermissionContext(mode="default", interactive=True)
    assert decide(ctx_i, "run_shell", {"command": "x", "escalate": True}).action == "confirm"
    ctx_n = PermissionContext(mode="default", interactive=False)
    assert decide(ctx_n, "run_shell", {"command": "x", "escalate": True}).action == "deny"


# ─── dispatch taxonomy ───────────────────────────────────────────────────────
def test_classify_capability():
    assert classify_capability("task_list") is Capability.META
    assert classify_capability("enter_plan_mode") is Capability.META
    assert classify_capability("agent") is Capability.AGENT
    assert classify_capability("skill") is Capability.SKILL
    assert classify_capability("memory") is Capability.MEMORY
    assert classify_capability("run_shell") is Capability.REAL
    assert classify_capability("read_file") is Capability.REAL


def test_router_allowlist_chokepoint():
    # 主 agent（None）→ 永不拦
    assert router_allowlist_blocks("run_shell", None) is False
    # 'agent' 对子 agent 一律拦（独立后备）
    assert router_allowlist_blocks("agent", frozenset({"agent", "read_file"})) is True
    # 纯宿主 meta 放行
    assert router_allowlist_blocks("task_list", frozenset()) is False
    assert router_allowlist_blocks("enter_plan_mode", frozenset()) is False
    # memory / skill / 真实工具：不在集内即拦
    assert router_allowlist_blocks("memory", frozenset({"read_file"})) is True
    assert router_allowlist_blocks("skill", frozenset({"read_file"})) is True
    assert router_allowlist_blocks("read_file", frozenset({"read_file"})) is False


def test_is_always_allowed_meta():
    assert is_always_allowed_meta("task_list")
    assert is_always_allowed_meta("exit_plan_mode")
    assert not is_always_allowed_meta("memory")
    assert not is_always_allowed_meta("agent")


def test_router_task_meta_uses_service_methods_without_task_manager():
    class Host:
        session_id = "s"
        is_sub_agent = False

        def tool_blocked_by_allowlist(self, name): return False
        def emit(self, event): return True
        def mint_tool_context(self, name):
            # Phase 3：host-routed task_* 经 ctx.tasks 把手薄转发回这些 service 方法。
            from nanocode.tools.context import ToolContext, TasksCap, RunsCap
            return ToolContext(tasks=TasksCap(self), runs=RunsCap(self))
        async def spawn_background_shell(self, command, timeout_ms): return "tid"
        def list_tasks(self, status=None, kind=None): return f"tasks:{status}:{kind}"
        def task_output(self, task_id, tail_bytes=8000): return f"output:{task_id}:{tail_bytes}"
        async def stop_task(self, task_id): return "stopped"
        async def recall_memory_semantic(self, query, limit): return ""
        async def spawn_memory_consolidate(self): return ""
        async def execute_plan_mode_tool(self, name): return ""
        async def execute_agent_tool(self, inp): return ""
        async def execute_skill_tool(self, inp): return ""
        async def run_real_tool(self, name, inp): return ""
        def hooks_suppressed(self): return True
        def has_active_hooks(self): return False
        def matching_hooks(self, event, tool): return []
        async def run_hook(self, hook, name, inp, result): return True, ""

    import asyncio
    router = CapabilityRouter()
    assert not hasattr(Host(), "task_manager")
    assert asyncio.run(router.dispatch(Host(), "task_list", {"status": "running"})) == "tasks:running:None"
    assert asyncio.run(router.dispatch(Host(), "task_output", {"task_id": "t1", "tail_bytes": 3})) == "output:t1:3"
