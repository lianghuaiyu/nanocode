"""SubAgentManager (CAP-P1)：子 agent 的并发 / 深度 / 超时 / turn 上限策略归口。

P4 的 containment 政策（max_threads 后台并发上限、max_depth 纵深 backstop、前台超时回退、
前台 turn 上限 clamp）从 Agent 抽到此处，作为 capability 边界化（doc12 Phase B / CAP-P1）的
第一步。Agent 持有一个实例（`self._subagents`），调用方直接 `host._subagents.*`
（docs/16 C-1：原 `Agent._depth_cap_exceeded` 等委托 shim 已删）。

spawn / run / artifact / result 机器仍在 Agent，后续增量迁入。本模块只持有对 agent 的反向
引用（duck-typed），**不 import engine**（避免循环）；load_agents_config 在调用时 import。
"""

from __future__ import annotations

# 前台子 agent 的回退 turn 上限：manifest 未声明 max-turns 时使用，确保前台子 agent 永远有界
# （不至无限循环拖死父 loop）。自 engine 迁入（CAP-P1）。
SUBAGENT_MAX_TURNS_FALLBACK = 50


class SubAgentManager:
    """子 agent 生成策略。当前只承载 caps；持有 `self.agent` 反向引用读取其 live 状态。"""

    def __init__(self, agent) -> None:
        self.agent = agent

    def running_background_count(self) -> int:
        """当前并发运行的后台子 agent 数（单账本 = child-owned run record，docs/25 A2）。

        所有后台子 agent（含 memory curator/eval）以 live coroutine 的 ``_nanocode_run_id`` +
        child run record 为准。host task（后台 shell / 扩展任务）不计入子 agent 并发上限。
        """
        n = 0
        for t in self.agent._background_tasks:
            run_id = getattr(t, "_nanocode_run_id", None)
            if not run_id:
                continue
            try:
                rec = self.agent._run_runtime.status(run_id)
            except Exception:
                continue
            if rec.status == "running":
                n += 1
        return n

    def depth_cap_exceeded(self) -> bool:
        """新 spawn 的子 agent 深度（agent.depth + 1）是否超过 max_depth。

        主 agent depth=0，其子 depth=1。今天子不能 spawn 孙（agent 工具被剥），故 live
        depth 结构上恒为 1；max_depth 是前瞻性纵深防御 backstop。"""
        from ..tools import load_agents_config
        max_depth = load_agents_config().get("max_depth")
        if not max_depth or max_depth <= 0:
            return False
        return (self.agent.depth + 1) > max_depth

    def max_threads(self) -> int:
        from ..tools import load_agents_config
        return load_agents_config().get("max_threads") or 0

    def background_cap_reached(self) -> bool:
        """后台子 agent 是否已达 max_threads 上限。curator/eval 与 agent 工具共用此判定，
        使「计入」与「受限」一致——否则 curator 计入计数却不受限，自相矛盾。"""
        mt = self.max_threads()
        return mt > 0 and self.running_background_count() >= mt

    @staticmethod
    def foreground_timeout(tool_timeout_ms, manifest_timeout_ms, fleet_cfg: dict):
        """前台子 agent 的有效超时：工具入参 > manifest 'timeout-ms'（profile.timeout_ms）>
        settings [agents] default_timeout_ms。全缺省 -> None（无 wall-clock 超时）。"""
        if tool_timeout_ms is not None:
            return tool_timeout_ms
        if manifest_timeout_ms is not None:
            return manifest_timeout_ms
        return fleet_cfg.get("default_timeout_ms")

    def bounded_max_turns(self, manifest_max_turns: int | None) -> int:
        """前台子 agent 的 turn 上限：manifest max-turns 优先，否则回退
        SUBAGENT_MAX_TURNS_FALLBACK；若父有剩余 turn 预算，clamp 到 min(value, parent_remaining)
        ——子绝不超过父。"""
        value = (manifest_max_turns if (manifest_max_turns and manifest_max_turns > 0)
                 else SUBAGENT_MAX_TURNS_FALLBACK)
        remaining = self.agent._parent_remaining_turns()
        if remaining is not None:
            value = min(value, remaining)
        return value
