"""capabilities/host.py — ToolHost：CapabilityRouter.dispatch 依赖的 typed port（docs/16 #5）。

把 dispatch 从「依赖 Agent god-class」改为「依赖一个显式协议」——这是解开
core/_execute_tool_call 锁链（docs/16 #1/#3）与 AgentProfile spawn cutover（#9）的结构前提。

诚实地宽（~17 成员）：真实工具派发确实需要 allowlist 判定、hooks、后台 spawn、task 面板与
plan/skill/agent 路由——这是工具派发的真实依赖面，不是意外耦合。Agent 结构性满足本协议
（runtime_checkable 结构检查由 tests/tools/test_spec.py 锚定）；任何替代宿主只需实现同一面。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolHost(Protocol):
    """CapabilityRouter.dispatch 的宿主依赖面。"""

    # ── 身份 / 状态 ──
    session_id: str
    is_sub_agent: bool
    task_manager: Any
    _background_tasks: Any
    _suppress_hooks: Any
    _active_hooks: Any

    # ── allowlist 咽喉点 + 遥测（emit：typed AgentEvent 单出口，docs/16 #2）──
    def _tool_blocked_by_allowlist(self, name: str) -> bool: ...
    def emit(self, event) -> bool: ...

    # ── 派发目标 ──
    async def _spawn_background_shell(self, command: str, timeout_ms) -> str: ...
    async def _recall_memory_semantic(self, query: str, limit: int) -> str: ...
    async def _spawn_memory_consolidate(self) -> str: ...
    async def _execute_plan_mode_tool(self, name: str) -> str: ...
    async def _execute_agent_tool(self, inp: dict) -> str: ...
    async def _execute_skill_tool(self, inp: dict) -> str: ...
    async def _run_real_tool(self, name: str, inp: dict) -> str: ...

    # ── hooks（pre/post-tool-use；fail-closed 门控在 _run_hook 内部）──
    def _matching_hooks(self, event: str, tool: str) -> list: ...
    async def _run_hook(self, hook: dict, name: str, inp: dict, result): ...
