# 25 · Route A→C 落地方案（清债 + 起隔板 + 编排扩展化 + C1/C2）

> 目标：0 上下文也能照本文落地。每条改动都标注 `file:line` 来源、改前→改后、测试、验收、回滚。
>
> 本文覆盖路线 **A（清债 + 起隔板）→ C（编排扩展化）+ C1（后台确认升级）+ C2（血缘正交）**。
> 基线分支 `nano/runtime-thread-boundary-cutover`，HEAD `90692b9`（= `b7cc88d` + 6 个 commit：`2c162f2`/`bd5030d`/`c8f2d8a`/`0ba7815` 落完 docs/23 Phase 3-6「起隔板」，`16876d5`/`90692b9` 进一步 lazy-ize core→③ import）。
>
> **起隔板（本文 §5）已全部落地**；本文剩余可落地范围 = Phase A 清债（§4）+ Phase C 编排扩展化（§6-8）。

## 0. 怎么用本文

- 行号以 HEAD `90692b9` + 当前工作树为准；动过文件后行号会漂移，按符号名定位。
- 测试统一用仓库 venv：`.venv/bin/python -m pytest ...`（等价 `PYTHONPATH=src python3 -m pytest ...`）。
- 每个阶段块结构固定：现状(来源) / 问题 / 目标 / 改法 / 数据结构 / 契约 / 测试 / 验收 / 回滚 / 不做。
- 证据分级：`Verified`(读过源码) / `Plan`(本文新设计) / `Decision`(需拍板)。

## 1. 分支现状基线（已落地 / WIP / 未落地）

| 项 | 状态 | 证据 |
| --- | --- | --- |
| ②a 裸主板 `AgentCore` 已抽离（`AgentLoopConfig` 注入） | ✅ 已落地（先于本分支） | `agent/core.py:38`、`agent/loop.py:20-58` |
| ① 模型层 `ProviderAdapter` 可换 | ✅ 已落地 | `agent/providers.py:63-88,218-224` |
| ③ `MemoryService`/`MemoryBackend` 可换 | ✅ 已落地（`7b34f42`） | `memory/service.py:39-68` |
| ④ `ExtensionHost` 注册式扩展（UNTRUSTED） | ✅ 已落地（`7b34f42`） | `extensions/host.py:156-181`、`extensions/api.py:33-66` |
| docs/23 Phase 0-2：`RuntimeThread` 私有化 `.agent/.session` | ✅ 已落地（`b7cc88d`） | `runtime/facade.py:442,450`、commit `b7cc88d` |
| docs/23 Phase 3：删 `agent/runtime.py`+`agent/session.py`、瘦身 `agent/__init__.py` | ✅ 已落地（`2c162f2`） | `agent/__init__.py:10`(`__all__=["Agent"]`)；两文件已删、无残留 import |
| docs/23 Phase 4：`_ensure_session_lease` fail-loud、`chat()` 内部化 | ✅ 已落地（`bd5030d`） | `engine.py:512`(fail-loud)、`engine.py:397`(`_chat_internal`) |
| docs/23 Phase 5：skill/hook 服务化 | ✅ 已落地（`0ba7815`） | `runtime/skill_service.py:33`(`SkillRuntimeService`)；`facade.invoke_skill` 改 delegate |
| docs/23 Phase 6：`can_switch()` 加强 | ✅ 已落地（`c8f2d8a`） | `runtime/facade.py:743`(已 gate `_live_run_ids()`)；pending-approval/`!shell` 有意未单列 |
| A-清债 D1 reserved-agent 断裂 | ❌ 未修 | `spawn.py:1355-1383` 调用不存在的 `task_manager.create_subagent` |
| A-清债 D2 双账本 | ❌ 未修 | `spawn.py:1056,1201` + `agent/subagent_manager.py:25-49` |
| A-清债 D5 wake 半契约 | ❌ 未修 | `tools/agent.py:35` + `runs/runtime.py:54-55` |
| A-清债 D8 O(N) 扫描热路径 | ❌ 未修 | `session/manager.py:349,372` ← `runtime/facade.py:830` |
| C 编排扩展化 | ❌ 未落地 | chain/parallel 仍焊在 `spawn.py:802,841` |
| C1 后台确认升级 | ❌ 未落地 | `spawn.py:60-62` auto-deny |
| C2 血缘正交 | ❌ 未落地 | `spawn.py:229-231` 混在一个 dict；listing 钩子 `listing.py:81-83` |

## 2. 源头真相图（anchor table）

| 关注点 | 当前位置 | 现状 | 目标层（四层设想） |
| --- | --- | --- | --- |
| 模型 I/O | `agent/providers.py` | 可换 ✅ | ① |
| 裸循环 | `agent/core.py` + `loop.py` | 可换 ✅ | ②a |
| harness（压缩/会话/系统提示词） | `engine.py`(god-class) + `session/agent.py` + `session/*` + `prompt.py` | 黏在 `Agent` 上 ❌ | ②b（焊契约、换实现） |
| 宿主服务 | `MemoryService`/`capabilities/*`/`CapabilityRouter` | 部分可换 | ③ |
| 对外控制面 | `runtime/facade.py` `RuntimeThread` | 已私有化 ✅，仍内部读 `_agent` 私有面 | ③ 边界 |
| 子 agent spawn 原语 | `runtime/spawn.py` `SubAgentRunner` | 在核 ✅（应留） | ③ 原语 |
| 编排（chain/parallel） | `runtime/spawn.py:802,841` | 焊进核 ❌ | 应移 ④ 扩展 |
| 扩展声明面 | `extensions/api.py` | 注册式 ✅ | ④ |
| 信任档 | `tools/types.py:48-54` | `SPAWN` 仅 BUILTIN | ③↔④ 边界（红线） |
| 事实源 | child `session.jsonl` | 权威 ✅；run_record 是派生 sidecar | ②b 契约 |

## 3. 四层戒律（每条改动必须满足）

- **G1 单向依赖**：① ← ②a ← ②b ← ③ ← ④（④只声明）。下层不 import 上层。
- **G2** ① 只做 LLM I/O，可换（`ProviderAdapter`）。
- **G3** ②a 只见 `AgentLoopConfig`，不碰 session/host。
- **G4** ②b 焊死的是契约（`session.jsonl` schema / turn lifecycle / event spine），实现可换；`session.jsonl` 是唯一事实源。
- **G5** ③ 实现可换（Protocol seam）；不得把事实源搬到 run_record/task。
- **G6** ④ UNTRUSTED、只声明不执行；不得拿 `SPAWN/fs/exec` 能力槽（`tools/types.py:48-54` 是红线）。
- **G7** 编排/multi-agent 属可卸能力，落 ③/④，不焊进 ②内核。

---

## 4. Phase A · 清债

> 目的：把"看起来能用其实断裂/误导"的路径改成真能用。不动 session 模型。可独立于其它 Phase 先落。

### A1 · 修 D1：reserved-agent 路径断裂（高危）`Verified`

- **现状/来源**：`runtime/spawn.py:1351-1383` 的 `run_reserved_agent` 调
  `host.task_manager.create_subagent(...)`（:1355）、`update_subagent(...)`（:1358/1376/1381）。
  但 `tasks/manager.py` 的 `TaskManager` **只有** `create_task/get_task/list_tasks/update_task/to_state/load_state`
  ——`create_subagent/update_subagent` 不存在。唯一调用方 `extensions/memory_evolution/agents.py:45-46` 把
  `Exception` 吞成 `return []`（":46 # diagnosis is best-effort"）。
- **问题**：retrieval 诊断子 agent 每次 spawn 即 `AttributeError`→被吞→恒返回 `[]`，诊断功能从不工作（裸契约漂移）。
- **目标**：reserved-agent 走与普通 subagent 一致的 run_record 体系，删幽灵调用。
- **改法**（`runtime/spawn.py` `run_reserved_agent`）：
  - 删除 `task_manager.create_subagent/update_subagent` 四处调用。
  - 改用既有 child-session run_record 机制：`new_child_session_id()` → `_build_sub_agent(... background=True, artifact_id=child_id)`
    → `begin_run_record(... agent_type=<reserved>, isolation="shared")` → 跑 `run_once` → `finish_run_record(...)` →
    `close_child_session`。完全复用 `run_memory_consolidate`（spawn.py:1067 起）的四态对称写法（completed/cancelled/timeout/error）。
  - 返回值仍是 `result["text"]`。
- **数据结构**：无新增；reserved-agent 复用 `subagent-run/` sidecar（`run_record.py`）。
- **契约**：`run_reserved_agent` 输入/输出不变；不再触 `TaskManager`。
- **测试**：新增 `tests/subagents/test_reserved_agent_run_record.py`：
  - spawn reserved agent → 不抛 `AttributeError`；产生 child `session.jsonl` + `subagent-run/status.json`，终态 `completed`。
  - `extensions/memory_evolution` 诊断路径返回非空（用桩 LLM）。
- **验收**：`.venv/bin/python -m pytest tests/subagents tests/agent/test_memory_optimize.py -q` 全绿；
  人工触发 `/memory optimize`（或扩展命令）能看到诊断输出而非静默 `[]`。
- **回滚**：单文件改动，`git revert` 该 commit。
- **不做**：不为兼容保留 `create_subagent` 空壳；不加 try/except 掩盖。

### A2 · 修 D2：双账本（run_record vs TaskManager）`Verified`

- **现状/来源**：`agent` 工具的 subagent → child-session run_record（`spawn.py:begin_run_record`）。
  但 memory curator/eval **同时**建 host task：`spawn.py:1056`（`create_task("memory_consolidate")`）、
  `spawn.py:1201`（`create_task("memory_eval")`）。并发计数 `subagent_manager.py:25-49` 必须同时数两套
  （`_nanocode_run_id` 走 run_record、`_nanocode_task_id` 走 TaskManager）。
- **问题**：`/agents`（看 run_record）与 `task_list/task_output`（看 TaskManager）互不可见；并发上限/取消语义分裂在两模型。
- **目标**：单账本——subagent 类工作（含 curator/eval/reserved）统一以 child-session run_record 为权威；
  TaskManager 仅留给**非 subagent** 的 host 任务（如后台 shell `tasks/runner.py`）。
- **改法**：
  1. `spawn.py` 的 `run_memory_consolidate`/`run_memory_eval` 删除 `create_task`/`update_task` 镜像，
     状态只写 run_record（已经在写 `begin_run_record`/`finish_run_record`，删冗余 host-task 那条腿）。
  2. `subagent_manager.py:running_background_count`（:25-49）简化为只数 run_record（`_nanocode_run_id`）；
     host-shell 任务另算（owner_agent_id 为 None 本就不计）。
  3. `RuntimeThread.agents_overview`（`runtime/facade.py:763`）已读 run_record，无需改；确认 `/memory` 命令的结果展示改读 run_record。
- **数据结构**：`tasks/models.py` 的 `memory_consolidate/memory_eval` kind 可保留给纯 host 进度，但**不再是 subagent 权威**。
- **契约**：`task_output(task_id)` 对 memory 任务仍可用（向后兼容查询），但权威状态在 run_record。
- **测试**：`tests/agent/test_memory_consolidate.py`、`test_memory_eval.py` 断言只产生一份权威记录；
  `running_background_count` 在仅 run_record 下正确。
- **验收**：`/agents` 能看到 curator/eval 运行；并发上限对二者一致生效。
- **回滚**：按 commit revert。
- **不做**：不把 run_record 权威搬给 TaskManager（违 G5）。

### A3 · 修 D5：wake 半契约 `Verified`

- **现状/来源**：`tools/agent.py:35` schema 宣传 `wake`（"whether to wake an idle child"）。
  但 `runs/runtime.py:54-55`：`if wake and rec.status != "running": raise RuntimeError(... use agent resume ...)`。
  即 `wake=True` 对非 running 子直接抛错，真正唤醒只能走 `resume`。
- **问题**：模型按 schema 以为能 `wake` idle 子，实际报错——契约与实现不符。
- **目标（已定 = 方案 1，删 wake）**：本质只有两个真实状态——live→steer(注入)、terminal→resume(重水合)。"wake 一个 idle 子"是不存在的第三态。
  - 方案 1（采纳）：从 `tools/agent.py` schema **删除 `wake`**，做对称两态契约。
  - 方案 2（不做）：实现真 wake——四家无一有此动词，等于自创，不做。
- **四家拍板依据（Pi/OpenCode/Codex 源码级 + Claude Code 文档级）**：
  - Pi：**无 wake**；只有 resume(`switchSession`)/new/fork/navigateTree + per-session steer/followUp（`agent-session-runtime.ts:193-221`）。
  - OpenCode：**无独立动词**；单一 `prompt`→`ensureRunning`：Running→attach(=steer)、Idle→新 run(=resume)（`effect/runner.ts:115-138`）。
  - Codex：**区分但只两态**——`turn/steer` 要求 active turn（`session/mod.rs:3653`，`NoActiveTurn`）；`resume` 重水合且**有 live writer 即拒**（`thread-store/src/local/mod.rs:146-162`）。
  - Claude Code：foreground(阻塞)/background(并发)+按 sessionId resume，**无 wake**。
  - 共识：只两态；nanocode 现有的 `wake`（叫醒非 running 子）正是四家都没有的尴尬中间态。
- **改法（方案 1）**：删 `tools/agent.py:35` 的 `wake` 属性 + 描述里 wake 字样；`runs/runtime.py:49-56` 的 `send()` 去掉 `wake` 参数；
  `subagents/steer.py:queue_steer` 的 `wake` 参数与 `wakeRequested` 字段一并清理；`spawn.py:execute_agent_tool` 中 `wake=bool(inp.get("wake"))`（约 :583）删除。
- **契约（Codex 式对称两态）**：`agent` 工具不再暴露 `wake`；`steer` 只对 **live** 子注入（无 live turn → 报错，仿 Codex `NoActiveTurn`）；`resume` 只对 **terminal** 子重水合（仍 live → 报错，仿 Codex "duplicate live writer"）。`runs/runtime.py:54-55` 已半成型（wake&非running 即抛），删 wake 后改成两态对称即可。
- **测试**：`tests/subagents/` 新增/改：steer 对 running 子排队成功；对 terminal 子报"use resume"；schema 不含 wake。
- **验收**：`rg "wake" src/nanocode/tools/agent.py` 无结果；steer/resume 契约测试绿。
- **回滚**：单点 revert。
- **不做**：不保留误导性 schema 字段。

### A4 · 修 D8：O(N) 全盘扫描热路径 + reconcile 写放大 `Verified`

- **现状/来源**：`session/manager.py:372 children()` → `:349 _scan_headers()` 遍历 `sessions_dir()` 下**所有** session
  读首行。`runtime/facade.py:830 _subagent_records()` → `runs/runtime.py:33 list()` → `ledger.py:48 reconcile_for_parent()`
  → `children()`。TUI footer/`/agents` 高频重绘每次都全盘扫描 + 逐 child 开 session + 读 sidecar。
- **问题**：两件独立的事，分两步治：
  - **(写放大/误判，P0)** reconcile 在重绘路径里对"非终态但无 live coroutine 的 run"写 `lost`（`engine.py:699-707 _reconcile_run` + `ledger.py reconcile_for_parent`）——只读重绘却产生写，且对 C1 的 `awaiting_approval` 子有误标风险（见 §7）。
  - **(O(N) 延迟，P1)** session 库一大，每次重绘全盘扫描即卡顿。
- **目标**：先把"重绘只读"做掉（小、收益确定）；派生索引只在**实测**确认扫描是瓶颈后再上（避免过早引入缓存一致性债，守 G5）。
- **改法 A4a（P0，先落）· 重绘只读**：
  1. 拆"只读列举"与"reconcile-mutate"两条路径：listing/footer/`/agents` 重绘走只读快照（读 header + sidecar，不写 `lost`）。
  2. `mark_lost` 只在显式生命周期事件（子结束、父主动 reconcile、resume 前）触发，不在重绘里。
  3. 验收：N 次 `/agents` 重绘对 `session.jsonl`/run_record **零写入**（计数写调用）。
- **改法 A4b（P1，实测后落）· 派生索引**：
  1. **前置 gate**：先量 N=500/1000 session 下重绘 `_scan_headers` 耗时；只有确认是热路径瓶颈才做下面。
  2. 新增 `session/child_index.py`：维护 `data_dir()/child-index.json`（或内存 LRU + 失效），标 `schemaVersion` + `derived: true`；
     `children(parent)` 先查 cache，miss 再 `_scan_headers()` 回填。
  3. 写者侧：header 写入含 subagent-parent 时增量更新；提供 `rebuild_child_index()` 全扫描重建；损坏即重建，不阻断。
- **契约**：`children()` 返回值语义不变。
- **测试**：A4a `tests/session/test_listing_readonly.py`（重绘零写）；A4b `tests/session/test_child_index.py`（cache==全扫描、删 cache 自动重建、并发不串）。
- **回滚**：A4a 合并两路径还原；A4b feature flag `NANOCODE_CHILD_INDEX=0` 回退纯扫描。
- **不做**：不把 child-index 当事实源；不让它决定 listing 可见性（仍读 canonical header，见 C2）；A4b 无实测瓶颈不做。

---

## 5. Phase A · 起隔板（docs/23 RuntimeThread/Agent 边界 cutover）

> 权威细节见 `docs/23`（Phase 0-6 + Commit 1-5）。**本节 5.0-5.4 已全部落地（基线 `b7cc88d` 之后的 6 个 commit）**，下列各节为落地对账 + 回归锚，非待办。
> **G1/G4 的隔板**：拆掉 `Agent` god-class 的"②b 固件 + ③ 服务 + 对外句柄"三重身份。

### 5.0 已落地（`b7cc88d`，docs/23 Phase 0-2 / Commit 1-2）`Verified`

- `RuntimeThread.agent/session` → `_agent/_session`（私有化）；新增 `attach_approvals()`（`runtime/facade.py:442`）、
  runtime-private `_agent_for_runtime()`（`runtime/facade.py:450`）。
- CLI/RPC 改走 `thread.attach_approvals(confirm_fn=...)`；`extensions/host.py` 读 `_agent`。
- 边界测试：`tests/runtime/test_runtime_thread_boundary.py`、`tests/entrypoints/test_runtime_facade_usage.py`。

### 5.1 Phase 3：删 runtime/session、瘦身 `agent/__init__.py` ✅ 已落地（`2c162f2`）

- **结果**：`agent/runtime.py`、`agent/session.py` 已删；`agent/__init__.py:10` 为 `__all__=["Agent"]`（lazy `__getattr__` 按需从 `.engine` 取 `Agent`）。`commands/types.py` 等 import 已改走 `from ...runtime import RuntimeThread`。
- **回归锚**：`git grep -n "nanocode\.agent\.runtime\|nanocode\.agent\.session" src tests` 仅命中 `*.egg-info/SOURCES.txt`（构建产物，非代码）；`import nanocode.agent` 不拉入 `nanocode.runtime`/provider SDK/`yaml`（`tests/entrypoints/test_command_import_boundaries.py`）。

### 5.2 Phase 4：runtime-owned lease + `chat()` 内部化 ✅ 已落地（`bd5030d`）

- **结果**：`engine.py:512 _ensure_session_lease()` 在 `_session_mgr is None` 时 `raise RuntimeError("No active session writer lease. Start the agent through AgentRuntime.")`，不再自取 lease；公开 `chat()` 改为 internal `_chat_internal`（`engine.py:397`，由 `run_once` 调）。
- **写者身份归 runtime**：`SessionLease.open_or_create` 仅余 runtime 层调用（`runtime/facade.py`、`runtime/spawn.py`），Agent 内部不再自取；子 agent 也由 spawn 注入。
- **回归锚**：无 runtime 注入直接走 turn → RuntimeError（`tests/session tests/subagents tests/runtime`）。

### 5.3 Phase 5：skill/hook 服务化 ✅ 已落地（`0ba7815`）

- **结果**：新增 `runtime/skill_service.py:33 SkillRuntimeService`（`resolve_user_invocation` + `install_hooks`）；`facade.invoke_skill()` 改为 delegate（不再直接 `self._agent._register_skill_hooks`）。
- **遗留 nuance（有意保留，非债）**：`_register_skill_hooks` 仍存于两处——`skill_service.py:74`（USER `/skill` 路径，新归口）、`engine.py:1039`（模型调用的 `skill` 工具路径，明确 out-of-scope）。
- **回归锚**：`rg "_register_skill_hooks" src/nanocode/entrypoints src/nanocode/tui` 为空（外部不再直接调）。

### 5.4 Phase 6：加强 `can_switch()` ✅ 已落地（`c8f2d8a`）

- **结果**：`runtime/facade.py:743 can_switch()` 在原 `is_processing` + `_background_tasks` 之外，新增 gate `self._agent._live_run_ids()`（live subagent 运行时返回 `(False, reason)`）；extension task 折进 `_background_tasks`。
- **有意未单列（docstring `runtime/facade.py:751-752` 记录）**：pending-approval 队列（经 `is_processing` 覆盖，因等审批的 turn 协程仍在跑）与 running `!shell`（REPL 串行 await，切换命令自然排队其后）。
- **与 C1 的交互（§7 已定，非待决）**：C1 的*非终态* `awaiting_approval` 后台子，其 turn 协程挂起期间**保持存活**（留在 `_background_tasks`），故自动计入 `_live_run_ids()` → `can_switch()` 天然挡住父切 session，**无需再改 can_switch**。详见 §7「与 `_live_run_ids()`/reconcile 的关键约束」(i)。
- **回归锚**：`tests/entrypoints/test_thread_lifecycle.py`、`tests/runtime/test_extension_context_lifecycle.py`、`tests/subagents`。

---

## 6. Phase C · 编排扩展化（把 chain/parallel 从内核移到 first-party 扩展）

> G7 落地：core 只出 `spawn_subagent` 单原语 + lifecycle 事件 + `orchestration_run` 派生契约；
> chain/parallel/planner/reviewer/fanout 作为可卸的 first-party 扩展。**B 的体验在此兑现，但不焊进内核。**

### C.0 · 稳定 core 原语（保留在 ③）`Plan`

- **保留不动**（这些是 ③ 原语，扩展复用）：`spawn.py:run_fresh_subagent`(:701)、`spawn_background_subagent`(:900)、
  `run_background_subagent`(:956)、权限派生 `agents/permissions.py:72`、sandbox 继承（`build_sub_agent` def :188、继承块 ~:224）、
  worktree（`subagents/worktree.py`）、run_record（`subagents/run_record.py`）。
- **新增 lifecycle 事件暴露**：把已有 `SubAgentStarted/SubAgentEnded`（`agent/events.py`）+ run_record 的
  created/started/completed/failed 结构化为**稳定 extension 事件**，经 `ExtensionAPI.on(...)`（`extensions/api.py:33`）可订阅。
- **契约**：`orchestration_run`（kind=chain/parallel/workflow）= run_record 的一种派生记录（`runs/models.py` 增 `kind` 字段），
  **非事实源**（守 G5），可重建。

### C.1 · 把编排逻辑搬出 spawn.py → first-party 扩展 `Plan`

- **现状/来源**：`spawn.py:565-572` 在 `execute_agent_tool` 里分派 `steps`(chain)/`tasks`(parallel) 到
  `execute_agent_chain`(:802) / `execute_agent_parallel`(:841)。
- **目标**：core 的 `agent` 工具只保留单 spawn（fresh/resume/background）；chain/parallel 由 first-party 扩展实现。
- **改法**：
  0. **盘点 schema 消费方（落地前必做）**：`steps`/`tasks` 是模型可见契约，删除前先 `rg "\"steps\"|\"tasks\"" src tests` + 排查所有 skill/workflow/测试里产出 chain/parallel 的调用方，逐一迁到新 `orchestrate` 工具/命令，避免删 schema 后既有提示词/技能静默失效。
  1. 新增 first-party 扩展 `extensions/orchestration/`（manifest 同 `memory_evolution`）。它 `register_task_kind("orchestration")`
     和/或 `register_command("/orchestrate")`，并声明一个 LLM 可见工具 `orchestrate`（`register_tool`，`extensions/api.py:48`）。
  2. 编排执行经 **host 受控接口**（见 C.2 决策）：扩展声明 steps/tasks → host 的编排 runner 逐步调
     `spawn_subagent` 原语并把 bounded envelope 回填（`{previous}` 替换逻辑从 `spawn.py:822` 搬进 runner）。
  3. 删 `spawn.py:802,841` 的 chain/parallel（或保留为 host runner 内部函数，但不再由 `agent` 工具直接触发）。
- **契约**：`agent` 工具 schema 去掉 `steps/tasks`（`tools/agent.py:38,43`）——**这是模型可见的契约变更**，blast radius = 所有产出 steps/tasks 的提示词/技能/测试（见改法 0），改由 `orchestrate` 工具/命令承载。
- **测试**：扩展 spawn 的子仍经 `effective_child_tools`（不可提权）；卸载扩展后 core 单 spawn 仍工作（G7）。
- **验收**：`rg "execute_agent_chain\|execute_agent_parallel" src/nanocode/tools src/nanocode/runtime/spawn.py`
  不再被 `agent` 工具直接调用；编排走扩展。
- **回滚**：扩展可整体禁用，回到单 spawn。
- **不做**：不在扩展里自己起子进程 spawn（违 G6）。

### C.2 · 信任决策（已拍板 = 路线 a）`Resolved`

> 纯 UNTRUSTED 扩展按 `tools/types.py:48-54` 拿不到 `SPAWN`，"编排扩展化"的驱动方式曾有两条路线，**经四家校准已定为 (a)**：

- **路线 (a) 声明式 workflow（推荐，零边界妥协，守 G6）**：扩展只注册 workflow 规格（steps/DAG），
  **host 的编排 runner（③）去 spawn**。扩展 handler 仍是 UNTRUSTED、不持 SPAWN。表达力 = host runner 支持的原语集。
- **路线 (b) 给 first-party 系统扩展更高信任档**：新增 `Trust.SYSTEM`（介于 BUILTIN/TRUSTED），
  `_TRUST_POLICY[SYSTEM]` 含 `SPAWN`，仅授予 in-tree 系统扩展（如 `extensions/orchestration`）。**动 G6，但只对自家代码**，第三方仍 UNTRUSTED。
- **四家拍板依据（Pi/OpenCode/Codex 源码级 + Claude Code 文档级）**：**没有一家**把 spawn 当成可发给第三方/untrusted 代码的自由能力。
  - Pi：扩展 API **无 spawn 原语**（`extensions/types.ts:1120-1348`）；"子 agent"=扩展起 `pi --mode json -p` 子进程（OS 隔离）。插件边界**不是**安全边界，信任只在资源加载层。
  - OpenCode：`task` 是 **core 内置工具、仅模型可调、按 `task` 权限 key 门控**（`tool/task.ts:104-114`）；插件只注册 tools/hooks，要 spawn 只能走公开 SDK/HTTP；子默认 deny `task` ⇒ 不能 spawn 孙（`agent/subagent-permissions.ts:14-27`）。
  - Codex：spawn 是普通工具，仅 `max_depth`/`max_threads` 限，**非信任档**；但子 config 从父派生、**父的 sandbox+approval 在任何 role/profile 之后最后重新覆盖**，故 profile **永远无法提权**（`multi_agents_common.rs:253-275`，应用顺序见 `multi_agents/spawn.rs:118`）。
  - Claude Code：spawn = 可信 agent 配置上的 `Agent` 工具白名单 + 硬深度上限 5；父 `bypassPermissions/acceptEdits` 优先，子不可覆盖。
- **判据（已定）**：四家共识 → **chain/parallel 走路线 (a)，并取消路线 (b)**。理由：(1) 本轮表达力需求（顺序 + fan-out + `{previous}` 替换）(a) 完全覆盖；(2) 四家无一给扩展发 SPAWN，新增 `Trust.SYSTEM`+SPAWN 是逆共识且破 G6。**spawn 永远 host-owned**，编排扩展经权限 key/`register_task_kind` **声明意图**触发（OpenCode 模型），handler 仍 UNTRUSTED。
- **落地附加约束（两条，来自 Codex/OpenCode，必须满足）**：
  - (i) **父上限最后重覆盖**（Codex）：子 agent 的 `effective_child_tools` + sandbox 必须在**任何 profile/role 层之后**再覆盖一次，确保 profile 无法提权。nanocode 已有 `agents/permissions.py:derive_child_profile` + depth/threads 双闸（`spawn.py:559-561 depth_cap_exceeded` + `host._subagents.max_threads()`），"上限"这块已与 Codex 对齐；**唯一缺口 = 补"profile/role 之后最后重覆盖"的顺序保证**。
  - (ii) **host-owned + 权限 key**（OpenCode）：core 暴露的 spawn 入口按权限 key 门控（如 `task`/`agent`），扩展不持 SPAWN 能力槽。
- **结论**：`Trust.SYSTEM` 分支**不做**（§11 已拍板）；第三方扩展永远 (a)。仅当未来要"运行时由扩展代码动态计算 DAG/决定 spawn 谁"才重新评估 (b)，且即便那时也优先扩 host runner 的声明式原语集而非发 SPAWN。

---

## 7. Phase C1 · 后台子 agent 确认升级回父 `Plan`

- **现状/来源**：`spawn.py:60-62 _auto_deny_confirm` 恒 `return False`——后台子 agent 危险操作一律 auto-deny-but-continue，父无从介入。
- **目标**：后台子需确认时进入 `awaiting_approval`（**非终态**），升级到父 `/agents`；父 allow/deny（单次），Esc 拒单次不杀子。
- **改法**：
  1. `runs/models.py`：新增**非终态** `awaiting_approval`（现有 `TERMINAL_RUN_STATUSES`(:13-20) 含 `blocked`——不要复用 blocked 作 pending；新增独立 awaiting_approval，且**不**加进 TERMINAL 集合）。
  2. **状态扩散点（逐个过，凡按 status 渲染/分支处都要识别新状态）**：`rg "TERMINAL_RUN_STATUSES"` 命中的渲染/逻辑点——`tui/subagent_widget.py`、`tui/tooltext.py`、`entrypoints/render.py`、`runs/ledger.py`、`runs/runtime.py`、`subagents/steer.py`、`tasks/models.py`——给 awaiting_approval 配图标/文案与分支处理。
  3. `subagents/run_record.py`：增 `pending_approval` 队列文件（仿 `pending_steer`，`run_record.py:46,292`）。
  4. `spawn.py`：后台子的 `confirm_fn` 从 `_auto_deny_confirm` 改为 `_escalate_confirm`：写 awaiting_approval + pending_approval 记录 + 挂起该子的 turn，发 NoticeRaised 升级事件。
  5. `runtime/facade.py`：`subagent_approve(child_session_id, allow: bool)` 控制面；`/agents` 页展示待确认 + allow/deny。父应答 → 写回 pending_approval 决议 → 子的 `confirm_fn` future resolve → 继续/拒绝单次。
  6. **暂停子 wall-clock 超时（Codex elicitation 模式，必做）**：进入 `awaiting_approval` 时**暂停该子的超时计时**，父应答后恢复；否则 `await_subagent_run`（`spawn.py:447`）的 `timeout_ms` 会在等人审批期间误杀子。仿 Codex 的 `thread/increment_elicitation`/`decrement_elicitation`（`app-server-protocol/.../common.rs:509-527`）——审批挂起期间不计入超时。
- **四家拍板依据（Pi/OpenCode/Codex 源码级 + Claude Code 文档级）**：现代方向一致 = 升级到父 + 阻塞等决定 + 标明请求者 + 拒单次不杀子 + 批准累积；离线一致 = fail-closed deny，绝不自动批。
  - Claude Code v2.1.186：后台子权限请求**浮到主会话**、标明哪个子在问、**Esc 拒单次不杀子**、批准**累积**（docs/sub-agents；feature #47339 "waiting for permission" 状态）。**这逐条就是本节设计。**
  - OpenCode：单一事件 ask——规则命中 allow/deny，否则发 `permission.asked` 事件 + **阻塞在 deferred** 等回复（前后台/子一视同仁）；未答仅在 teardown `RejectedError`，**不自动批**（`permission/index.ts:83-111,65-72`）。
  - Codex：turn **PAUSE**（注册 oneshot + 发 `ExecApprovalRequest` + await，`session/mod.rs:2090-2150`）；`Never`(非交互)=**auto-deny 回模型，绝不升级/自动跑**（`protocol.rs:877-879`）；并有 elicitation 计数暂停超时（见改法 6）。
  - Pi：非交互=auto-deny(block+error result，agent 继续)；仅 RPC 模式 escalate 到 client，超时 default deny（`examples/.../permission-gate.ts:19-23`；`rpc-mode.ts:141-144`）。
- **与 `_live_run_ids()`/reconcile 的关键约束（必须满足）**：`_live_run_ids()`（`engine.py:689`）按**内存活跃协程**（`_background_tasks` 中 `not task.done()`）判定，**不按 status**。两条推论：
  - (i) awaiting_approval 期间该子 turn 协程应**保持存活**（await 在父决议 future 上、留在 `_background_tasks`），从而自动计入 `_live_run_ids()` → `can_switch()`（§5.4）自然挡住父切 session（= 推荐语义，**无需再改 can_switch**）。
  - (ii) **陷阱**：`_reconcile_run`（`engine.py:699-707`）对"非终态 + 不在 `_live_run_ids()`"的 run 写 `mark_lost`；若 awaiting_approval 实现成协程退出，会被**误标 lost**。故要么走 (i) 让协程挂起期间存活（推荐，天然规避），要么在 `_reconcile_run`/`reconcile_for_parent` 显式把 awaiting_approval 排除出 mark_lost。此项与 §4 A4a「重绘只读」同源，合并考虑。
- **数据结构**：run_record `status=awaiting_approval`（非终态）；`pending_approval.jsonl`（派生）。
- **契约**：后台 confirm 不再静默 deny；父在线 → 升级并阻塞等批；**无父通道 / 超过短窗口（建议 60–120s）→ fail-closed deny**（四家一致，绝不自动批）。nanocode 已有的 `capabilities/permissions.py:75-77`（`interactive=False ⟹ deny`）就是离线兜底——C1 只是把"立即 deny"升级成"先 escalate、无通道/超时再 deny"。
- **测试**：后台 coder 触发危险操作 → awaiting_approval；父 allow → 真执行；父 deny/超时 → 拒单次不杀子；
  child 不污染顶层 listing 仍成立。
- **验收**：后台 `git push` 类操作可在父侧获批后执行；且 awaiting_approval 期间子**不被 `mark_lost` 误标、不被 wall-clock 超时杀**（两条误杀路径见改法 6 + 约束块）。
- **回滚**：flag 回退到 `_auto_deny_confirm`。
- **不做**：不做无人值守自动批准；不让 pending 队列无限堆积（设上限）。

---

## 8. Phase C2 · parent ⟂ fork 血缘正交 `Plan`

- **现状/来源**：`spawn.py:229-231` 把 `{sessionId, entryId, taskId, agentId}` 混在一个 `parentSession` dict。
  顶层 listing 排除判据是 `listing.py:81-83`：`if ps and ps.get("agentId"): continue`（subagent 子不进列表）。
  fork/clone 血缘也复用同一 dict。
- **问题**：subagent 血缘与 fork 血缘语义混用；未来 fork 子若误带 agentId 会被错误隐藏，反之亦然。
- **目标**：header 拆 `subagentParent`（subagent 专用，listing 据此隐藏）⟂ `forkLineage`（fork/clone 专用，不影响 listing）。
- **改法**：
  1. `spawn.py:build_sub_agent`（:229-231）写 `subagentParent={sessionId,entryId,childSessionId,agentType}`。
  2. fork/clone producer（`runtime/facade.py` thread_fork/clone、`session/*`）写 `forkLineage={fromSessionId,fromEntryId}`。
  3. `listing.py:81-83` 改判据：`if ps and ps.get("subagentParent"): continue`（只按 subagent 隐藏）。
  4. `runs/ledger.py:children()`/`session/manager.py:children()` 按 `subagentParent` 收集 child。
- **迁移**：旧 header 的 `parentSession.agentId` → 一次性 producer 迁移到 `subagentParent`；**不留 alias/双读兼容**（守用户约束 + G4）。
  提供一次性 `migrate_session_headers()` 脚本，按 canonical 重写 header（producer fix，非读取兜底）。
- **数据结构**：session header 新增 `subagentParent`/`forkLineage`；删 `parentSession`（迁移后）。
- **契约**：listing 可见性钩子从 `agentId` 改 `subagentParent`；child 收集同步改。
- **测试**：`tests/session/test_session_listing*`：subagent 子不进顶层 listing、fork 子进列表；
  迁移脚本幂等；无 `parentSession` 残留。
- **验收**：`rg "parentSession" src tests` 仅出现在迁移/历史注释；新代码用两字段。
- **回滚**：迁移前打快照 tag；迁移脚本可逆（保留 pre-migration 备份）。
- **不做**：不保留 `parentSession` 双命名兼容（违用户硬约束）。

---

## 9. 依赖与落地顺序

```text
A1 (D1 reserved) ─┐
A2 (D2 ledger)   ─┤  互相独立, 可并行先落
A3 (D5 wake)     ─┤
A4 (D8 ro+idx)   ─┘
       │
起隔板 5.1→5.4  ✅ 已全部落地 (2c162f2/bd5030d/0ba7815/c8f2d8a) → C 系列 ③ 接缝前置已满足
       │
C.0 (core 原语+lifecycle 事件)            # 依赖起隔板的 ③ 接缝稳定
       │
C.2 (已定=a，§11) ──→ C.1 (编排扩展化)     # 依赖 C.0
       │
C1 (确认升级)   # 依赖 C.0 的 run_record 扩展 + facade 控制面
C2 (血缘正交)   # 可与 C 并行; 依赖 A4 的 children/listing 钩子稳定
```

- **关键前置**：C 系列依赖的"起隔板"③ 接缝（`RuntimeThread`/`spawn` 原语）已稳（5.1-5.4 落地），可直接进 C.0。
- **可先见效**：A1-A3 立即修正确性（A4 见 §4 重新拆分建议：先做 listing/footer 重绘只读，index 暂缓）。

## 10. 测试与验证矩阵

```bash
# 干净度
git diff --check

# focused（按 Phase 选）
.venv/bin/python -m pytest tests -k "subagent or spawn or run_record or runs or listing or steer or facade" -q
.venv/bin/python -m pytest tests/runtime tests/entrypoints tests/session tests/subagents -q

# import 边界（起隔板）
git grep -n "nanocode\.agent\.runtime\|nanocode\.agent\.session\|from nanocode\.agent import .*Runtime\|from nanocode\.agent import .*AgentSession" src tests   # 期望空
.venv/bin/python -c "import sys, nanocode.agent; assert not [m for m in sys.modules if m.startswith(('anthropic','openai','yaml','nanocode.runtime'))]"

# full
.venv/bin/python -m pytest -q
```

必须覆盖的验证点：
1. session artifact：spawn 后 child `session.jsonl` + `subagent-run/{status.json,events.jsonl,result.md}` 一致。
2. permission/sandbox 继承：`effective_child_tools` = 父 allow∩ / 子 deny∪ / 剔 agent；child `sandbox_profile==父`。
3. background：C1 前 auto-deny；C1 后 awaiting_approval + 父可应答；审批期间子超时暂停、不被 `mark_lost`，离线短窗口后 fail-closed deny。
4. child 不污染顶层 listing（C2 后按 `subagentParent` 隐藏、fork 子可见）。
5. result/cancel/resume/steer：bounded envelope；cancel 对 live、`mark_lost` 对无 coroutine；resume 拒 running、接终态；steer live 注入；wake 已删（A3）。
6. worktree：parallel coder/general 自动 worktree；finalize 写 diff；分支不泄漏。
7. D1：reserved-agent 不抛 AttributeError、诊断返回非空。
8. D8：child-index cache == 全扫描；删 cache 自动重建。
9. 扩展 spawn 的子不可提权（C）；卸载编排扩展后单 spawn 仍工作（G7）。

## 11. 拍板结论（四家源码依据已回填，Decision → Resolved）

> 三个待决策已用 Pi/OpenCode/Codex（源码级）+ Claude Code（文档级）四家做法校准并定下。逐条依据见对应阶段节。

| 拍板点 | 结论 | 四家依据（要点） | 落在本文 |
| --- | --- | --- | --- |
| **C.2 spawn 信任** | **走路线 (a)；取消 `Trust.SYSTEM`+SPAWN**。spawn 永远 host-owned + 权限 key；子继承父上限**最后重覆盖**；扩展只声明 | 四家无一给（第三方）扩展 SPAWN：Pi 无 spawn API/走子进程；OpenCode core 工具 + `task` 权限 key；Codex 普通工具但父策略最后覆盖不可提权；CC 工具白名单 + 深度 cap | §6 C.2 |
| **A3 wake** | **删 wake（方案 1）**，做对称两态契约（steer=live / resume=terminal） | Pi/OpenCode/CC 无 wake；Codex 区分但只两态（steer 需 active turn、resume 拒 live writer）。共识=只两态 | §4 A3 |
| **C1 离线超时** | **短窗口（建议 60–120s）后 fail-closed deny**；并新增"审批期间暂停子超时" | 离线四家一致 fail-closed deny（Pi/Codex auto-deny、OpenCode RejectedError、CC fail-closed）；Codex elicitation 计数暂停超时 | §7 C1 |

剩余真·未决（非本三点）：C.1 编排扩展化里 `orchestrate` 工具的最终 schema 形状（steps/DAG 表达力边界）——待 C.0 原语稳定后定。

## 12. 不做事项（全局）

- 不引入 SQLite/第二事实源（违 G4/G5）。
- **不给任何扩展 SPAWN 能力槽，不新增 `Trust.SYSTEM`**（四家共识 + 违 G6）；spawn 永远 host-owned + 权限 key。
- **不做独立 `wake` 动词**（四家无一有；只留 steer/resume 两态）。
- 不保留 `parentSession`/旧 import 的双命名兼容 alias（违用户硬约束）。
- 不让 TUI 绕过 facade 直读底层文件。
- 不做对等 swarm（子 spawn 孙）/code-mode JS vm/子进程委派（本轮范围外）。
- **后台审批离线时不自动批**（四家一致 fail-closed deny）。
- 不为白盒测试保留 public `.agent`。
