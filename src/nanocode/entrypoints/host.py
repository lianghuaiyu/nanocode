"""RuntimeHost —— REPL 的会话宿主（docs/14 P1）。

把"当前 thread"从 `run_repl` 的固定局部闭包里解放出来：命令 handler 永远对 **当前** thread
操作，lifecycle 替换（/new /resume /clone /fork、子父导航）由 runtime 原子换掉整组
Agent/AgentSession/RuntimeThread，而 handler 无需缓存它们。

- `context()` 每次 dispatch 前重新生成 CommandContext，绑定 `current_thread` 的 agent/session/sink。
- `replace_thread(new)` 切到新 thread，并 dispose 旧 thread 的 registry 注册（session 状态的
  finalize/rebuild 归 `Agent.rebind_session`，P2）。
- `can_switch()` fail-closed：turn 运行中 / 有后台任务 / 有 running|idle 子 agent 时拒绝切换
  （docs/14 §3.3；child-session P6 之前一律 fail-closed）。
"""

from __future__ import annotations

from .commands.types import CommandContext


class RuntimeHost:
    def __init__(self, runtime, thread, *, registry=None, interactive=True) -> None:
        self._runtime = runtime
        self._current_thread = thread
        self._registry = registry
        self._interactive = interactive

    @property
    def runtime(self):
        return self._runtime

    @property
    def current_thread(self):
        return self._current_thread

    def context(self) -> CommandContext:
        """每次 dispatch 重新生成——handler 永远看到当前 thread 的 agent/session/sink，
        替换 thread 后无需通知任何 handler（它们不缓存 agent/session）。"""
        t = self._current_thread
        return CommandContext(agent=t.agent, session=t.session, out=t.agent._sink,
                              registry=self._registry, interactive=self._interactive)

    def replace_thread(self, new_thread) -> None:
        """切到新 thread；register 新 thread（保证 registry 始终含当前 thread）+ dispose 旧 thread。

        session 级状态的 finalize（tracer.close / _auto_save / 后台任务 / persist sandbox）与
        rebuild 都在 `Agent.rebind_session` 内完成（P2）；本方法只换 wrapper 并维护 registry。
        register 幂等——调用方即便已注册过也安全；不再依赖"调用方先注册"的约定。"""
        old = self._current_thread
        self._runtime.register(new_thread)
        self._current_thread = new_thread
        if old is not None and old is not new_thread:
            old.dispose()

    def can_switch(self) -> "tuple[bool, str | None]":
        """lifecycle 切换前的 fail-closed 闸。返回 (允许?, 拒绝原因)。

        直接读 agent 状态、不吞异常——本闸的全部价值在于 fail-CLOSED；若 list_subagents 等
        抛错，宁可让它炸成响亮的 bug，也绝不静默 default 成"无子 agent → 允许切"（fail-open）。"""
        a = self._current_thread.agent
        if a.is_processing:
            return False, "a turn is currently running"
        if a._background_tasks:
            return False, f"{len(a._background_tasks)} background task(s) still running"
        busy = [s for s in a.task_manager.list_subagents() if s.status in ("running", "idle")]
        if busy:
            return False, f"{len(busy)} sub-agent(s) still running/idle"
        return True, None
