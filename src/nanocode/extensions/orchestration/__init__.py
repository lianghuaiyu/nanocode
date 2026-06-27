"""orchestration — first-party 编排扩展（layer④，docs/26 §0.6 阶段1）。

chain/parallel 编排**策略**从内核上提到此处（可装可卸）；内核只保留 spawn 原语（经受信槽
ctx.spawn）+ 委托。`activate` 注册唯一 orchestrator；内置 `agent` 工具的 steps/tasks 经
engine._run_orchestration → host.run_orchestrator 委托到 `policy.orchestrate`。
"""
