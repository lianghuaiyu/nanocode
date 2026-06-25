# 22 · EvolveMem 接入 nanocode 的源码审计与落地计划

状态：设计规格草案（2026-06-16）

目标：在不破坏 nanocode 可嵌入式边界的前提下，吸收 SimpleMem/EvolveMem 的自进化检索思想，把当前 `/memory optimize` 从“显式 unavailable”升级为真实可用的 host-owned retrieval optimizer。本文档必须在 0 上下文情况下可落地。

## 0. 一句话结论

EvolveMem 值得接，但不能整体移植。

上游 EvolveMem 的强点是闭环：

```text
eval questions
  -> retrieval / answer / scoring
  -> failure diagnosis
  -> candidate retrieval config
  -> guarded promotion / revert
  -> history + next round
```

nanocode 应该保留这个闭环，把它改造成：

```text
host-owned MemoryService
  -> no-LLM SimpleMemEngine.retrieve_fast hot path
  -> confirmed eval set
  -> deterministic offline optimizer
  -> optional locked sub-agent diagnosis
  -> deterministic promotion gate
  -> atomic retrieval_config.json
```

不要把上游 `simplemem/evolver/*` 整包放回 `nanocode.memory.engines.simplemem`。它有自己的 store、policy、telemetry、benchmark adapter、auto-upgrade worker、LLM answering loop、review queue 和 `~/.simplemem` 默认路径，直接接入会冲掉 docs/20 已经建立的 embedded/runtime boundary。

## 1. 参考源码

### 1.1 上游 SimpleMem / EvolveMem

审计源码：`aiming-lab/SimpleMem` main 分支，commit `74174a1c0c95d487144a74a77addb2c23f27f783`。

关键文件：

| 文件 | 用途 |
|---|---|
| [`simplemem/evolver/__init__.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/__init__.py) | EvolveMem public facade，导出 manager/store/extractor/evolution/multi-retriever/replay/promotion/self-upgrade worker。 |
| [`simplemem/evolver/config.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/config.py) | `EvolveMemConfig`，默认路径是 `~/.simplemem` / `~/.simplemem/records`，并派生 store/policy/telemetry path。 |
| [`simplemem/evolver/optimize.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/optimize.py) | `simplemem.optimize()` degraded mode：只拿现有 memories + dev questions 调全局 retrieval config，不是 paper-faithful EvolveMem。 |
| [`simplemem/evolver/evolution.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/evolution.py) | 完整 self-evolution loop：Extract -> Index -> Retrieve -> Answer -> Evaluate -> Diagnose -> Adjust -> Repeat。 |
| [`simplemem/evolver/multi_retriever.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/multi_retriever.py) | `RetrievalConfig` action space + BM25/semantic/structured/fusion/MMR/intent planning 等检索面。 |
| [`simplemem/evolver/diagnosis.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/diagnosis.py) | LLM failure diagnosis：从 QA failures 生成参数建议，含 hard step-size、prior attempts、防重复 rejected move。 |
| [`simplemem/evolver/meta_analysis.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/meta_analysis.py) | 跨 round meta analyzer：stagnation、regression、focus subcategory、new dimension proposal。 |
| [`simplemem/evolver/candidate.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/candidate.py) | bounded candidate generation，用小网格扰动 policy。 |
| [`simplemem/evolver/replay.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/replay.py) | offline replay evaluator，比较 baseline/candidate retrieval policy。 |
| [`simplemem/evolver/promotion.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/promotion.py) | deterministic promotion gate：sample count、metric deltas、zero retrieval 增量约束。 |
| [`simplemem/evolver/self_upgrade.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/self_upgrade.py) | controlled self-upgrade orchestrator：candidate files、reports、review queue、promotion。 |
| [`simplemem/evolver/upgrade_worker.py`](https://github.com/aiming-lab/SimpleMem/blob/74174a1c0c95d487144a74a77addb2c23f27f783/simplemem/evolver/upgrade_worker.py) | background worker：周期性 auto-upgrade，带 review queue / health state。 |

### 1.2 Pi extension 参考源码

审计源码：本地 `/private/tmp/pi-src`，commit `3fcfb7abf77784bfe52567f7670870834efb65d1`。

Pi 的 extension 机制不是一个单点 `Plugin` 类，而是四层组合：

```text
package / directory discovery
  -> extension factory receives ExtensionAPI
  -> factory registers contributions into runner registries
  -> runner emits lifecycle events with call-time ExtensionContext
  -> tools / commands / hooks cross host-owned runtime boundaries
```

关键文件：

| 文件 | 对 nanocode 的启发 |
|---|---|
| `/private/tmp/pi-src/packages/coding-agent/docs/extensions.md:108-180` | extension 来源分全局、项目、本地显式路径；项目 extension 必须在 trust 后加载；extension 默认导出 factory，factory 内调用 `pi.on` / `pi.registerTool` / `pi.registerCommand`。 |
| `/private/tmp/pi-src/packages/coding-agent/docs/extensions.md:219-224` | factory 不应启动长生命周期资源；后台资源要等 `session_start` 或命令/工具实际需要时创建，并在 `session_shutdown` 清理。EvolveMem optimizer worker 也应绑定 runtime/session，而不是 import 时启动。 |
| `/private/tmp/pi-src/packages/coding-agent/docs/extensions.md:272-342` | lifecycle event 是 extension 的主轴：`session_start`、`resources_discover`、`before_agent_start`、`context`、`tool_call`、`tool_result`、`session_shutdown` 等都走 runner。nanocode 第一版不必全开，但内置 memory evolution extension 应使用同一种事件/注册机制。 |
| `/private/tmp/pi-src/packages/coding-agent/docs/extensions.md:1269-1281` | `registerTool()` 可在 load、`session_start` 或命令 handler 中调用，并刷新当前 session 工具表；工具通过 schema、prompt snippet/guidelines、执行函数进入 host。nanocode 可借鉴 `ToolDefinition` 形态，但 memory optimize 第一版不应注册给 LLM 的普通工具。 |
| `/private/tmp/pi-src/packages/coding-agent/docs/extensions.md:1371-1385` | `appendEntry(customType, data)` 用于持久化 extension state，但不进入 LLM context。nanocode 不能把 EvolveMem history 写进 session tree；应写入 memory store root 的 optimize history。 |
| `/private/tmp/pi-src/packages/coding-agent/docs/extensions.md:1164-1231` | session replacement / reload 后旧 `pi` 和旧 `ctx` 会 stale；后续工作必须使用新 ctx。nanocode 的 `ExtensionContext` 也必须 call-time 生成，不能让 extension 捕获旧 `MemoryService` 或旧 `RuntimeThread`。 |
| `/private/tmp/pi-src/packages/coding-agent/docs/packages.md:114-132` | package 的 `pi` manifest 只声明资源入口：`extensions`、`skills`、`prompts`、`themes`，路径相对 package root。nanocode 可以借鉴 manifest + contribution 的分离，但第一版 system extension 不开放 npm/git/package installer。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/types.ts:300-333` | `ExtensionContext` 暴露 cwd、readonly session manager、model registry、当前 model、abort signal、context usage、compact 等 host 能力；session manager 是 read-only。nanocode 的 memory extension context 应同样只读 session，并额外暴露受控 `MemoryService`。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/types.ts:339-365` | `ExtensionCommandContext` 才有 `newSession` / `fork` / `navigateTree` / `switchSession` 等更强 session 控制。nanocode 应把“命令上下文”和“一般事件/工具上下文”分开；EvolveMem 不需要 session mutation 权限。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/types.ts:435-470` | `ToolDefinition` 是 extension tool 的契约：name/label/description/schema/execute，execute 接收 `ExtensionContext`。nanocode future `register_tool` 应包装到 CapabilityRouter，而不是直接绕过 permission/sandbox。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/types.ts:1120-1180` | `ExtensionAPI` 统一承载 `on(...)`、`registerTool(...)`、`registerCommand(...)`。nanocode 不要把 memory optimize 单独塞进 CLI；应让内置 extension 通过同一个 API 注册 slash command 和 task kind。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/types.ts:1487-1588` | `ExtensionRuntime` 保存 actions、active/stale 状态、registered tools/commands/handlers；runtime action 在 bind 后才可调用。nanocode 的 ExtensionHost 也应先收集贡献，再绑定 runtime services。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/loader.ts:120-170` | extension runtime 初始 actions 是 throwing stubs；`registerTool()` 这类注册动作可在 load 阶段有效，真正 host action 要等 runner bind。nanocode 应避免 extension import 阶段访问 `MemoryService`。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/loader.ts:177-225` | `createExtensionAPI()` 把 `on/registerTool/registerCommand/registerShortcut/registerFlag` 写入 extension object 的 registry，并调用 runtime refresh。nanocode 可做 Python-native `ExtensionAPI`，但禁止第一版 system extension 注册快捷键/UI。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/loader.ts:445-604` | loader 从 `package.json` 的 `pi.extensions` 或 `index.ts/js` 发现入口；项目 `.pi/extensions`、全局 extensions、显式路径统一加载。nanocode 第一版只加载 package 内置 manifest，例如 `src/nanocode/extensions/memory_evolution/manifest.py`，未来再开放项目/用户路径。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/runner.ts:262-379` | runner 持有 extensions/runtime/cwd/sessionManager/modelRegistry，`bindCore()` 把 runtime actions 和 context actions 注入，并 flush pending provider registrations。nanocode 的 ExtensionHost 应在 `RuntimeServices.create` 后 bind memory/task/model/event 能力。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/runner.ts:613-725` | `createContext()` / `createCommandContext()` 在调用时生成带 stale check 的 ctx，命令 ctx 比普通 ctx 能力更强。nanocode 应禁止 extension 把 ctx 长期缓存；每次 command/task/hook 执行都重建 context。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/extensions/runner.ts:736-1044` | runner 按事件串联 handler；部分事件可 block/modify/chained transform。EvolveMem 第一版只需要 `session_start`、`session_shutdown`、`memory_generation_end`、`command`、`task` 这几个窄事件，不开放 tool result/context rewrite。 |
| `/private/tmp/pi-src/packages/coding-agent/src/core/tools/tool-definition-wrapper.ts:4-18` | extension tool 被 wrapper 成 core `AgentTool`，execute 时由 wrapper 注入 ctx。nanocode future custom tools 也必须通过 `tools/spec.py` + `PermissionEngine`/CapabilityRouter 包装，不可直接给模型一条裸 Python callback。 |
| `/private/tmp/pi-src/packages/coding-agent/examples/extensions/dynamic-tools.ts:24-73` | dynamic registration 示例说明 extension 可在 session_start 或 command handler 注册工具。EvolveMem 可借这个思想动态启用诊断 agent/tools，但默认不把 optimizer 暴露为 LLM-callable tool。 |
| `/private/tmp/pi-src/packages/coding-agent/examples/extensions/subagent/index.ts:454-490` | Pi 的 subagent 例子是 extension tool，自行发现 agents 并可 parallel/chain。nanocode 不应照搬这个 `--no-session`/外部进程式子 agent；应复用现有 reserved hidden sub-agent + TaskManager，并保持 child session/permission 边界。 |
| `/private/tmp/pi-src/packages/agent/src/harness/session/memory-repo.ts`、`memory-storage.ts` | 这里的 `memory` 是 session repo/storage 的内存实现，不是长期用户 memory。Pi 当前可参考 extension/session runtime，不可参考长期 memory 产品形态。 |

必须借鉴：

1. **factory + API 注册**：extension import/load 只登记贡献，不直接执行 host 业务。
2. **registry + runner**：commands/tools/handlers/task kinds 都进入 host registry，由 runner 调用。
3. **call-time context**：ctx 由 runner 每次构造，session replacement 后旧 ctx 失效。
4. **readonly session owner**：extension 可以读 session/context，但 session tree owner 仍是 runtime/session。
5. **trust/capability 分层**：项目/用户 extension 需要 trust；system extension 可内置启用，但仍走相同 registry。

必须拒绝：

1. 第一版不做任意 TS/Python project extension loader。
2. 第一版不开放 provider payload rewrite、UI widget、shortcut、message renderer。
3. EvolveMem 不注册为普通 LLM-callable tool，不允许模型在 turn hot path 自行 optimize。
4. 不把 Pi 的 session `memory-repo` 当长期 memory 参考。

### 1.3 当前 nanocode 源码

关键文件：

| 文件 | 当前事实 |
|---|---|
| `src/nanocode/extensions/` | 当前不存在；nanocode 还没有 Pi-style extension host/runner/manifest，只有 skills/hooks/MCP/subagents/commands 等局部扩展面。 |
| `docs/20-simplemem-memory-extension-refactor.md` | 已明确 SimpleMem 是 nanocode-owned internal engine，不保留 vendor compatibility；`evolver/*` 面过大，只能 host-controlled maintenance 使用。 |
| `docs/05-memory.md` | 当前 memory 架构文档：`MemoryService` host-owned，`SimpleMemEngine` 不 import runtime/session/capabilities，热路径 `retrieve_fast` no-LLM。 |
| `src/nanocode/memory/service.py` | `MemoryService` 是 host 与 long-term memory 的唯一边界；prefetch 和 tool search 都走 `backend.retrieve_fast`。 |
| `src/nanocode/memory/simplemem_backend.py` | 适配 `MemoryService` 到 internal `SimpleMemEngine`，store root 是 `{agent_dir}/memory/simplemem/{project_hash}`。 |
| `src/nanocode/memory/engines/simplemem/config.py` | 当前 `SimpleMemConfig` 只有 storage/extraction/top_k，不支持 evolved retrieval policy。 |
| `src/nanocode/memory/engines/simplemem/retriever.py` | 当前 `retrieve_fast` 是 semantic + lexical + deterministic structured + fixed RRF；不调用 LLM。 |
| `src/nanocode/memory/engines/simplemem/vector_store.py` | LanceDB store 暴露 semantic/keyword/structured search，但返回 `MemoryEntry`，没有显式 score object。 |
| `src/nanocode/memory/generate.py` | host-owned generation pipeline：直接调用 engine，无 sub-agent，无 memory tool/MCP/network，按 entry-id watermark 增量。 |
| `src/nanocode/memory/eval_store.py` | 已有 pending/confirmed/rejected QA eval store；human confirmation 是硬约束。 |
| `src/nanocode/memory/maintenance.py` | 仍残留 markdown-centric `evolve_config.json` lifecycle 与 eval/prune 逻辑。 |
| `src/nanocode/runtime/spawn.py` | `/memory eval generate` 使用内置 eval curator；`/memory optimize` 当前创建 task 后报告 EvolveMem unavailable。 |
| `src/nanocode/subagents/prompts.py` | `memory-eval-curator` 是 hidden reserved sub-agent，无工具，只输出 QA JSON。 |
| `src/nanocode/agents/registry.py` | reserved system agents 不允许被项目 `.nanocode/agents` 覆盖，也不向模型暴露为普通 spawn type。 |

## 2. 上游 EvolveMem 机制拆解

### 2.1 它不是一个小优化函数

`simplemem/evolver/__init__.py` 暴露的是整套系统：

```text
MemoryManager / MemoryStore / MemoryConsolidator
MemoryExtractor / EvolutionEngine / MemoryDiagnostics
MultiViewIndex / RetrievalConfig / retrieve_multiview
MemoryReplayEvaluator / should_promote
MemorySelfUpgradeOrchestrator / MemoryUpgradeWorker
```

这说明 upstream 的 EvolveMem 不只是“调 top_k”。它会拥有存储、策略、telemetry、候选策略、replay records、review queue、后台 worker。对 nanocode 来说，这些生命周期必须属于 host/runtime，不属于 `SimpleMemEngine`。

### 2.2 `simplemem.optimize()` 本身是 degraded mode

上游 `simplemem/evolver/optimize.py` 在文件头明确说它不是 paper-faithful entry point。它把用户传入的 `(question, answer)` dev set 和已有 memories 输入给 `EvolutionEngine`，禁用了 fresh extraction、benchmark-specific prompts、per-category adapter 语义。

它做的事：

```text
resolve mem backend
wrap llm_call
wrap embedder
convert existing memories to dicts
convert dev questions to qa_pairs
EvolutionEngine.evolve(sessions=[], qa_pairs, initial_memories)
return Config(best retrieval params)
```

这个 degraded mode 和 nanocode 更接近，因为 nanocode 也应该先优化现有 SimpleMem index 的 retrieval policy，而不是让 optimizer 重新抽取 session 或拥有 memory store。

### 2.3 Paper-faithful `EvolutionEngine` 太重

`evolution.py` 的主循环是：

```text
Phase 1: Extract memories
Phase 2: Build MultiViewIndex
Phase 3: Answer all questions
Phase 4: Score
Phase 5: Diagnose
Phase 6: Elitist accept/reject
Phase 7: Adjust config
Phase 7b: Meta-analysis
Phase 8: Targeted extraction for gaps
Finalize best config
```

这个 loop 同时调用 LLM 做 extraction、answer generation、failure diagnosis、query decomposition、intent planning、answer verification。它还写 `cache_dir` / `results_dir`。这些能力在 benchmark harness 里合理，但不能直接进入 nanocode turn hot path，也不应该直接落在 `SimpleMemEngine` 里。

可借鉴的机制：

1. `EvolutionConfig.elitist=True`：candidate 只有超过 acceptance threshold 才算 accepted。
2. `attempt_history`：把 rejected moves 回灌给 diagnosis，防止重复试同一个坏配置。
3. `max_changes_per_round`：每轮最多改少量字段，便于归因。
4. `best_config` / revert：promotion 是有门槛的，不是 LLM 说好就用。
5. raw per-question report：每轮保留 question、prediction、reference、retrieved_sources、metrics。

### 2.4 `RetrievalConfig` 是真正应该移植的核心

`multi_retriever.py` 里的 `RetrievalConfig` 把 action space 显式化：

```text
semantic_top_k / keyword_top_k / structured_top_k / max_context
fusion_mode = first_found | weighted_sum | rrf | *_only
weight_semantic / weight_keyword / weight_structured
time_decay_half_life_days
reflection_rounds
per_category_overrides
enable_query_decomposition / intent_planning / coverage_reflection
enable_kg_expansion
enable_answer_verification
adapter prompt flags
answer_model / answer_model_ensemble
mmr_diversity_weight / mmr_candidate_pool
```

nanocode 不应该一次性接全部字段。第一版只接 no-LLM retrieval policy 字段：

```text
semantic_top_k
keyword_top_k
structured_top_k
max_context
fusion_mode
weight_semantic
weight_keyword
weight_structured
structured_person_weight
structured_entity_weight
timestamp_weight
time_decay_half_life_days
lexical_exact_boost
```

暂不进第一版：

```text
reflection_rounds
query_decomposition
intent_planning
coverage_reflection
answer_verification
answer_model / ensemble
benchmark adapter prompt flags
```

原因：这些都会引入 LLM call、answer-generation policy 或 benchmark-specific surface。它们可以存在于离线 eval/diagnosis 实验里，但不能污染 nanocode 的 `retrieve_fast` 热路径。

### 2.5 上游还有一条更适合 nanocode 的 conservative path

`candidate.py` + `replay.py` + `promotion.py` + `self_upgrade.py` 比 `EvolutionEngine` 更贴近 nanocode：

```text
generate bounded candidate policies
  -> offline replay evaluation
  -> compare baseline vs candidate
  -> should_promote deterministic gate
  -> optional review queue
  -> promote policy file
```

这个模式的正确性来源是 host gate，不是 LLM 自评。nanocode 应该优先抄这条，而不是优先抄 `EvolutionEngine` 的 LLM diagnosis loop。

## 3. 当前 nanocode 支持现状

结论：当前 nanocode 对 EvolveMem 是“有残留接口，无真实支持”。

已经有的：

1. `/memory optimize` 命令入口存在。
2. task kind `memory_optimize` 存在。
3. `eval_store.py` 有 pending/confirmed/rejected QA candidates。
4. `/memory eval generate` 可以用 `memory-eval-curator` 生成候选，host 填 `session_id`，人工 confirm/reject。
5. `SimpleMemEngine.retrieve_fast()` 是 no-LLM hybrid，可作为优化目标。

缺失或不符合 SimpleMem backend 的：

1. `runtime/spawn.py::run_memory_optimize()` 直接报告 unavailable。
2. `maintenance.py` 的 `evolve_config.json` 仍是旧残留，路径与 active SimpleMem project-hash store 不一致。
3. `build_eval_curator_message()` 仍读 markdown `project_memory_dir()`，不是 backend-aware。simplemem backend 下 eval candidate source 不稳定。
4. `SimpleMemConfig` 没有独立 `RetrievalConfig`，也没有从 store root 加载 evolved config。
5. `Retriever` 只有固定 RRF 权重和 fixed top_k，优化空间太小。
6. `VectorStore` search 返回 `MemoryEntry`，没有 score/rank metadata；第一版可用 rank-based RRF，后续如要 weighted_sum/MMR 需要 store 返回 scored hits 或暴露 vectors。

## 4. 设计原则

### 4.1 嵌入式边界是硬约束

不能破坏 docs/20 的边界：

```text
AgentCore 不 import memory
AgentSession 是唯一 session tree 写入者
MemoryService 是 host-owned boundary
SimpleMemEngine 只做 memory algorithm/index
LLM/embed 只能 host 注入
子 agent 默认无 MemoryService
后台 memory worker 必须 host 启动并能力锁死
```

EvolveMem 接入后仍必须成立：

1. Turn hot path `retrieve_fast` 不调用 LLM。
2. `/memory optimize` 是 host task，不是模型 tool action。
3. Candidate promotion 由 deterministic host gate 执行。
4. LLM/sub-agent 只能提建议或生成待确认 eval，不能写 live retrieval config。
5. 外部上下文 polluted 的 session 不自动生成 eval/memory；除非用户显式确认。
6. 不做 markdown fallback，不做 `auto` backend，不做 silent `[]`。

### 4.2 两条权力线必须分开

```text
read/use line:
  MemoryService.start_prefetch
  MemoryService.execute_tool(search/read/list/stats)
  SimpleMemEngine.retrieve_fast

write/optimize line:
  MemoryGenerationPipeline
  eval candidate generation
  human confirmation
  RetrievalOptimizer
  promotion gate
  retrieval_config.json
```

`retrieve_fast` 可以读取 promoted config，但不能知道是谁优化的，也不能触发优化。

### 4.3 多 agent 可以用，但不能让 agent 自证

多 agent 适合这几个位置：

| 角色 | 是否推荐 | 权限 |
|---|---:|---|
| Eval Curator | 已有，保留并 backend-aware | 无工具，只输出 QA candidates，host 填 provenance，人类 confirm。 |
| Failure Diagnostician | 可选，第二阶段接 | 无 memory write，无 MCP/network/shell，只读 optimize report，输出 JSON suggestions。 |
| Meta Analyst | 可选，第三阶段接 | 只读 history，输出 new-dimension proposal，不自动改代码/配置。 |
| Retrieval Judge | 谨慎，仅作 fallback | 只在 exact/evidence scoring 不够时离线判分，不参与 promotion gate 的唯一依据。 |
| Promotion Agent | 不允许 | promotion 必须 deterministic host gate。 |

## 5. 推荐架构

### 5.0 Extension-first 承载形态

如果目标是“作为 extension 接入 nanocode”，推荐把 EvolveMem 做成 **受控 system extension**，不是普通用户可随意覆盖的项目插件，也不是 `SimpleMemEngine` 内部模块。

这里要向 Pi 源码看齐的是 extension **形状**，不是照搬 Pi 的 TypeScript 任意代码加载器：

```text
Pi:
  discover package/dir entry
  -> default factory(pi: ExtensionAPI)
  -> pi.on/registerTool/registerCommand writes registries
  -> ExtensionRunner.bindCore(...)
  -> runner creates call-time ExtensionContext
  -> host-owned session/tool/model runtime executes

nanocode:
  discover built-in system extension manifest
  -> activate(api: ExtensionAPI)
  -> api.register_command/register_task_kind/register_hidden_agent/on
  -> ExtensionHost.bind_runtime(...)
  -> host creates call-time ExtensionContext
  -> RuntimeThread/TaskManager/MemoryService executes
```

#### 5.0.1 Pi-aligned extension 四层

第一层：manifest / discovery。

Pi 从 `.pi/extensions`、`~/.pi/agent/extensions`、settings、package `pi.extensions` 发现入口。nanocode 第一版不开放项目/用户目录，只加载内置 system extension，但 manifest 仍要保留 Pi 式资源入口语义：

```python
@dataclass(frozen=True)
class ExtensionManifest:
    id: str                         # "nanocode.memory_evolution"
    kind: Literal["system"]         # future: "user" | "project"
    entrypoint: str                 # "nanocode.extensions.memory_evolution:activate"
    contributes: ExtensionContributes
    capabilities: frozenset[str]    # {"memory:read", "memory:optimize", "task:create", "model:diagnose"}
```

`contributes` 不是运行时代码，只声明 contribution surface：

```python
@dataclass(frozen=True)
class ExtensionContributes:
    commands: tuple[CommandContribution, ...] = ()
    task_kinds: tuple[str, ...] = ()
    hidden_agents: tuple[str, ...] = ()
    lifecycle_events: tuple[str, ...] = ()
    model_roles: tuple[str, ...] = ()
```

第二层：activation factory。

向 Pi 的 `export default function(pi: ExtensionAPI)` 对齐，nanocode 用 Python-native `activate(api: ExtensionAPI) -> None`。activation 只能登记贡献，不能启动后台 worker、不能读取 env、不能拿 `MemoryService`：

```python
def activate(api: ExtensionAPI) -> None:
    api.register_command(
        CommandContribution("/memory optimize", match="exact"),
        run_memory_optimize_command,
    )
    api.register_command(
        CommandContribution("/memory eval generate", match="exact"),
        run_memory_eval_generate_command,
    )
    api.register_task_kind("memory_optimize", run_memory_optimize_task)
    api.register_hidden_agent(memory_retrieval_diagnostician)
    api.on("memory_generation_end", maybe_schedule_eval_generation)
    api.register_model_role("memory_diagnosis", ModelRolePolicy(default="small"))
```

这对应 Pi 源码里的 `createExtensionAPI()`：`on/registerTool/registerCommand` 写入 extension object registry；真正 host action 等 runner bind 后才可执行。

第三层：runner / registries。

新增 `ExtensionHost` 不是业务模块，而是 registry + lifecycle dispatcher：

```text
ExtensionHost
  manifests
  extensions
  command_registry
  task_kind_registry
  hidden_agent_registry
  lifecycle_handlers
  model_role_registry
```

它需要支持：

1. `load_system_extensions()`：只加载 package 内置 manifest。
2. `activate_all()`：调用 `activate(api)` 收集贡献。
3. `bind_runtime(RuntimeThread, RuntimeServices)`：注入 call-time context factories。
4. `command_registry()`：交给 CLI/TUI/RPC command dispatcher 合并。
5. `run_task(kind, payload)`：由 `TaskManager` 触发，不让 extension 自己创建裸 asyncio task。
6. `emit(event)`：串联生命周期 handler，错误 fail loud 进 diagnostics/task result。

第四层：call-time context。

向 Pi 的 `ExtensionContext` / `ExtensionCommandContext` 分层对齐：

```python
class ExtensionContext:
    cwd: str
    thread: RuntimeThread
    session: ReadOnlySessionView
    memory: MemoryService | None
    tasks: TaskManagerView
    models: ExtensionModelRouter
    events: EventSink
    signal: AbortSignal | None

class ExtensionCommandContext(ExtensionContext):
    async def wait_for_idle(self) -> None: ...
```

第一版故意不提供：

1. `new_session` / `fork` / `switch_session`。
2. `set_active_tools` / provider rewrite。
3. UI widget / renderer / shortcut。
4. direct `append_session_entry`。

EvolveMem 需要的状态写入不走 session tree，而走 memory store root：

```text
{NANOCODE_HOME}/memory/simplemem/{project_hash}/retrieval_config.json
{NANOCODE_HOME}/memory/simplemem/{project_hash}/optimize/history.jsonl
{NANOCODE_HOME}/memory/simplemem/{project_hash}/optimize/runs/<run_id>/
```

#### 5.0.2 EvolveMem extension 的正确边界

```text
nanocode extension host
  loads built-in memory-evolution manifest
  activates memory_evolution.activate(api)
  registers slash commands / hidden agents / task kinds / lifecycle handlers
  binds call-time ExtensionContext to RuntimeThread + RuntimeServices

memory-evolution extension
  owns optimizer orchestration, reports, agent prompts, model routing
  proposes retrieval configs
  never writes session tree directly
  never bypasses CapabilityRouter / task manager / MemoryService

SimpleMemEngine
  owns only retrieval algorithm and index operations
  reads promoted RetrievalConfig supplied by host
```

建议目录形态：

```text
src/nanocode/extensions/
    __init__.py
    api.py                  # ExtensionAPI: registration-only surface
    context.py              # ExtensionContext / ExtensionCommandContext
    host.py                 # ExtensionHost / bind_runtime / emit / registries
    manifest.py             # typed manifest schema + contribution types
    registry.py             # command/task/agent lifecycle registries
    errors.py               # ExtensionLoadError / ExtensionRuntimeError
    memory_evolution/
        __init__.py
        manifest.py         # built-in system extension manifest
        extension.py        # activate(api)
        commands.py         # /memory optimize, /memory eval generate wiring
        tasks.py            # memory_optimize task handler
        agents.py           # hidden agent profiles + prompts
        models.py           # per-role model routing
        optimizer.py        # host-only deterministic optimizer
        reports.py          # report/history persistence
```

第一版 manifest 不追求 Pi 的任意代码 extension，但 contribution 名称要和 Pi 的 mental model 对齐：

```python
ExtensionManifest(
    id="nanocode.memory_evolution",
    kind="system",
    entrypoint="nanocode.extensions.memory_evolution.extension:activate",
    contributes=ExtensionContributes(
        commands=(
            CommandContribution("/memory optimize", match="exact"),
            CommandContribution("/memory eval generate", match="exact"),
        ),
        task_kinds=("memory_optimize",),
        hidden_agents=("memory-retrieval-diagnostician", "memory-meta-analyst"),
        lifecycle_events=("memory_generation_end", "session_shutdown"),
        model_roles=("memory_diagnosis", "memory_meta_analysis"),
    ),
    capabilities=frozenset({
        "memory:read",
        "memory:evaluate",
        "memory:write_retrieval_config",
        "task:create",
        "model:diagnose",
    }),
)
```

#### 5.0.3 为什么不是普通 tool / MCP / skill

1. **不是普通 tool**：Pi 的 `registerTool()` 是给 LLM 调用的工具面；EvolveMem optimize 会写长期 retrieval config，不能让模型在 hot path 里自由触发。
2. **不是 MCP server**：MCP 适合外部工具/资源；EvolveMem 需要 host-owned `MemoryService`、confirmed eval store、TaskManager 和 atomic promotion，不该跨进程绕过 runtime policy。
3. **不是 skill**：skill 是 prompt/resource 扩展；EvolveMem 是 lifecycle + task + model-role + persistence 扩展。
4. **不是 engine 内部模块**：`SimpleMemEngine` 必须保持 no-LLM hot path 和无 runtime/session import；optimizer orchestration 属于 extension host。

这能让 EvolveMem 在产品身份上是 extension，在安全身份上仍是 built-in system capability。后续如果开放项目/用户 extension，可以复用同一 manifest/API/runner，但需要额外 trust、capability allowlist、sandbox 和 disable UI。

### 5.1 Runtime plane

```text
RuntimeServices.create
  -> MemoryService(config, cwd, agent_dir, llm, embed)
  -> SimpleMemBackend
  -> load retrieval_config.json from active SimpleMem store root
  -> SimpleMemEngine(SimpleMemConfig(..., retrieval=RetrievalConfig))
  -> Retriever.retrieve_fast(query, limit)
```

特点：

1. Config 文件属于 active SimpleMem store root：

```text
{NANOCODE_HOME}/memory/simplemem/{project_hash}/retrieval_config.json
{NANOCODE_HOME}/memory/simplemem/{project_hash}/optimize/history.jsonl
{NANOCODE_HOME}/memory/simplemem/{project_hash}/optimize/runs/<run_id>/
```

2. `maintenance.evolve_config_path()` 的旧路径不再作为 source of truth。
3. `SimpleMemBackend` 负责加载 host-resolved config 并传给 engine。
4. Engine 可持有 immutable `RetrievalConfig`，但不读 env、不读 cwd、不自己决定路径。

### 5.2 Offline optimize plane

```text
/memory eval generate
  -> backend-aware eval input
  -> memory-eval-curator proposes candidates
  -> host add_pending
  -> human /memory eval confirm <id>

/memory optimize
  -> host task kind memory_optimize
  -> require active backend == simplemem
  -> require confirmed eval count >= threshold
  -> load baseline retrieval_config
  -> generate bounded candidates
  -> evaluate baseline + candidates on confirmed eval set
  -> promotion gate
  -> atomic save retrieval_config.json only if improved
  -> write report + history
```

第一版不使用 LLM diagnosis。这样可以先把系统闭环跑通，避免 agent 自评风险。

### 5.3 Optional diagnosis plane

第二阶段再加：

```text
/memory optimize --diagnose
  -> run deterministic baseline/candidates
  -> if no promotion or stagnation
  -> spawn hidden memory-retrieval-diagnostician subagent
  -> subagent reads summarized failure report only
  -> returns strict JSON suggestions over allowlisted fields
  -> host validates suggestions
  -> host turns suggestions into candidate configs
  -> host re-evaluates
  -> host promotion gate
```

注意：diagnostician 的输出永远只是 candidate source，不是 config mutation。

### 5.4 多模型 / 多 agent 的推荐分工

EvolveMem 很适合用多模型，但不要让所有角色都用最强模型。推荐按职责路由：

| 角色 | 形态 | 推荐模型 | 频率 | 说明 |
|---|---|---|---|---|
| Eval Curator | hidden sub-agent | cheap/fast structured model | 手动 `/memory eval generate` | 从 memory entries 生成 QA candidates；只输出 JSON；人类确认。 |
| Retrieval Evaluator | host Python | no model | 每次 optimize | 跑 baseline/candidates，计算 deterministic score。 |
| Failure Diagnostician | hidden sub-agent | reasoning model | candidate 全失败或分数停滞时 | 读失败摘要，提出 allowlisted config suggestions。 |
| LLM Judge | optional hidden sub-agent/service | high-precision judge model | 仅 ambiguous cases | 不能作为唯一 promotion signal，只能补充 evidence/answer containment 判断。 |
| Meta Analyst | hidden sub-agent | reasoning model 或便宜模型 | 多轮 history 后低频触发 | 提出“新增 action dimension”建议，例如 MMR、time decay、query planning。 |
| Promotion Gate | host Python | no model | 每次 optimize | 唯一有权写 live `retrieval_config.json`。 |

模型路由不要写死在 extension agent prompt 里，放到 extension model policy：

```python
MemoryEvolutionModelPolicy(
    eval_curator_model="fast-structured",
    diagnostician_model="reasoning",
    judge_model="judge",
    meta_model="reasoning",
    max_parallel_judges=4,
)
```

如果 runtime 当前只暴露一个 `host.model`，先在 extension 内做“可选覆盖但 fail loud”：

```text
NANOCODE_MEMORY_EVOLVE_EVAL_MODEL
NANOCODE_MEMORY_EVOLVE_DIAG_MODEL
NANOCODE_MEMORY_EVOLVE_JUDGE_MODEL
NANOCODE_MEMORY_EVOLVE_META_MODEL
```

这些 env 只能由 host/CLI 读取，不能由 `SimpleMemEngine` 读取。

### 5.5 多 agent 性能优化

多 agent 不是越多越好。建议并行化放在两层：

1. **候选评测并行**：host Python 对 candidate configs 并行跑 confirmed eval set。这里没有模型调用，收益最大、风险最低。
2. **失败诊断分片**：当失败案例很多时，把 worst failures 按 category/topic 分给多个 diagnostician shard，各自输出 suggestions，再由 host merge/dedup。

不推荐：

```text
agent A 评 candidate 1
agent B 评 candidate 2
agent C 投票决定谁 promoted
```

原因：这会把最关键的 promotion 交给 LLM 自评，且 token cost 高、复现差。

推荐流程：

```text
1. Host deterministic evaluator finds top failing buckets.
2. If no candidate passes gate:
   spawn N diagnostician shards over failure buckets.
3. Host validates each JSON suggestion.
4. Host materializes suggestions into candidate configs.
5. Host deterministic evaluator re-runs candidates.
6. Host promotion gate writes or rejects.
```

Shard merge 规则：

```text
unknown field -> reject
out-of-range value -> reject
same field conflicting values -> keep none unless one suggestion has stronger eval evidence
more than max_changes_per_round -> rank by historical lift and truncate
suggestion repeats rejected move -> reject
```

这样多 agent 提升的是“候选生成质量”和“失败解释质量”，不是替代 deterministic replay。

## 6. 方案列表

### 方案 A：Deterministic Host Optimizer（推荐第一版）

内容：

1. 添加 nanocode-native `RetrievalConfig`。
2. 改 `Retriever` 支持 config-driven no-LLM fusion。
3. 用 confirmed eval set 做 offline retrieval scoring。
4. 用 bounded grid/candidate perturbation 找更好 config。
5. promotion gate 通过才落 `retrieval_config.json`。

优点：

1. 最符合 embedded boundary。
2. 不引入新 LLM 热路径。
3. 不需要 upstream `evolver/*` 运行时依赖。
4. 容易写单测。
5. 与上游 conservative self-upgrade path 一致。

缺点：

1. 没有 EvolveMem paper 那种“自动发现新维度”的能力。
2. 评分依赖 confirmed eval 质量。
3. 初期 action space 比上游小。

适用结论：必须先做。

### 方案 B：多 agent EvolveMem-style Diagnosis（推荐第二版）

内容：

1. 保留方案 A 的 deterministic optimizer/promotion。
2. 增加 hidden `memory-retrieval-diagnostician`。
3. 失败时让 agent 读 per-question failure summary，输出 JSON suggestions。
4. Host 把 suggestions 变成候选 configs 再评测。
5. 可再加 `memory-meta-analyst` 读 history，提出 new-dimension proposal。

优点：

1. 接近上游 `diagnosis.py` / `meta_analysis.py` 精髓。
2. 可以解释为什么当前配置失败。
3. 对复杂检索缺陷更有帮助。

风险：

1. Agent 可能提出不在 action space 的字段。
2. Agent 可能过拟合小 eval set。
3. 如果让 agent 直接写 config，会形成 self-validation。

控制手段：

1. JSON schema allowlist。
2. `max_changes_per_round`。
3. rejected move history。
4. train/validation split。
5. promotion host-only。

适用结论：做完方案 A 后再做。

### 方案 C：直接移植 upstream `simplemem.evolver`

内容：把 upstream `simplemem/evolver/*` 整包或大部分接进 nanocode。

结论：不推荐。

原因：

1. 上游 `EvolveMemConfig` 默认 `~/.simplemem`，路径/lifecycle 与 nanocode data root 不一致。
2. 上游 manager/store/policy/telemetry 会和 `MemoryService` 职责重叠。
3. `EvolutionEngine` 同时做 extraction、answering、diagnosis、cache/results 写入，边界过宽。
4. 引入 rank_bm25/numpy/sentence-transformers/benchmark adapter 等依赖面。
5. 容易把 LLM retrieval planning 带进 hot path。
6. 与“SimpleMem 是 internal engine，不做 upstream compatibility”方向冲突。

可以复制算法，不应该复制 ownership。

### 方案 D：只开放 `retrieve_planned`，不做 optimizer

内容：让 `memory search` 或某个显式 action 使用 LLM planned retrieval。

结论：可作为工具增强，但不是 EvolveMem 接入。

原因：

1. 没有 eval/promotion/history。
2. 不能自适应配置。
3. 可能把 LLM 引入用户可感知延迟。

### 方案 E：外部 research harness

内容：把 upstream EvolveMem 保持在 repo 外，通过导出 SimpleMem memories 和 confirmed evals 跑实验，再手动导入 config。

结论：可做 research，不应作为产品默认路径。

原因：边界清晰，但用户体验差，且无法保证 config schema/路径/governance 一致。

## 7. 文件级落地计划

### Phase 0 · Pi-aligned ExtensionHost 骨架

目标：先把 extension 承载面做出来，再把 EvolveMem 接进去。不要直接在 `runtime/spawn.py` 里继续追加 memory optimize 逻辑，否则会把 system extension 退化成普通内置函数。

新增：

```text
src/nanocode/extensions/__init__.py
src/nanocode/extensions/api.py
src/nanocode/extensions/context.py
src/nanocode/extensions/errors.py
src/nanocode/extensions/host.py
src/nanocode/extensions/manifest.py
src/nanocode/extensions/registry.py
src/nanocode/extensions/memory_evolution/__init__.py
src/nanocode/extensions/memory_evolution/manifest.py
src/nanocode/extensions/memory_evolution/extension.py
```

`manifest.py`：

```python
@dataclass(frozen=True)
class CommandContribution:
    name: str
    match: Literal["exact", "exact_or_prefix"] = "exact"
    description: str = ""
    arg_hint: str = ""

@dataclass(frozen=True)
class ExtensionContributes:
    commands: tuple[CommandContribution, ...] = ()
    task_kinds: tuple[str, ...] = ()
    hidden_agents: tuple[str, ...] = ()
    lifecycle_events: tuple[str, ...] = ()
    model_roles: tuple[str, ...] = ()

@dataclass(frozen=True)
class ExtensionManifest:
    id: str
    kind: Literal["system"]
    entrypoint: str
    contributes: ExtensionContributes
    capabilities: frozenset[str] = frozenset()
```

`api.py`：

```python
class ExtensionAPI:
    def on(self, event: str, handler: LifecycleHandler) -> None: ...
    def register_command(self, spec: CommandContribution, handler: ExtensionCommandHandler) -> None: ...
    def register_task_kind(self, kind: str, handler: ExtensionTaskHandler) -> None: ...
    def register_hidden_agent(self, profile: HiddenAgentProfile) -> None: ...
    def register_model_role(self, role: str, policy: ModelRolePolicy) -> None: ...
```

第一版不要实现 `register_tool()`，或者只定义类型并 fail loud。原因：EvolveMem 不需要 LLM-callable tool；提前开放会扩大攻击面。后续实现 custom tools 时，必须像 Pi `tool-definition-wrapper.ts` 那样包进 nanocode `CapabilityRouter`/`PermissionEngine`。

`context.py`：

```python
@dataclass(frozen=True)
class ExtensionContext:
    cwd: str
    thread: RuntimeThread
    session: ReadOnlySessionView
    memory: MemoryService | None
    tasks: TaskManagerView
    models: ExtensionModelRouter
    events: EventSink
    signal: AbortSignal | None = None

@dataclass(frozen=True)
class ExtensionCommandContext(ExtensionContext):
    wait_for_idle: Callable[[], Awaitable[None]]
```

实现要求：

1. context 必须每次 command/task/hook 调用时由 `ExtensionHost.create_context()` 生成。
2. context 内的 `session` 只能是 `ReadOnlySessionView`。
3. context 不暴露 raw `Agent`、raw `_session_mgr`、raw `_background_tasks`。
4. session replacement / resume / fork 后，旧 ExtensionHost 或旧 ctx 必须不可继续使用；可以用 generation id 或 `active` flag fail loud。
5. import/activation 阶段不能访问 `memory` / `thread` / `tasks`，只能注册贡献。

`host.py`：

```python
class ExtensionHost:
    @classmethod
    def load_system_extensions(cls) -> "ExtensionHost": ...
    def activate_all(self) -> None: ...
    def bind_runtime(self, thread: RuntimeThread, services: RuntimeServices) -> None: ...
    def invalidate(self, reason: str) -> None: ...
    def command_registry(self) -> Registry: ...
    async def run_task(self, kind: str, payload: dict, *, task_id: str) -> None: ...
    async def emit(self, event: ExtensionEvent) -> None: ...
```

注册冲突规则先简单、fail loud：

1. system extension command 与 builtin command 同名：如果 builtin 没有显式标记 `replaceable=True`，启动失败。
2. 两个 system extension 注册同一 task kind：启动失败。
3. hidden agent type 与 reserved/custom agent type 冲突：启动失败。
4. lifecycle handler error：写 diagnostics；task handler error：task failed；command handler error：返回 local error。

修改：

```text
src/nanocode/runtime/facade.py
src/nanocode/entrypoints/host.py
src/nanocode/entrypoints/commands/builtin.py
src/nanocode/entrypoints/commands/registry.py
src/nanocode/tasks/models.py
```

落地步骤：

1. `RuntimeServices` 增加 `extension_host: ExtensionHost | None`，在 `RuntimeServices.create()` 构造 MemoryService 后加载/activate/bind system extensions。
2. `RuntimeThread` 暴露 `extension_host` 只读属性，并在 thread/session replacement 时 invalidate 旧 host，再为新 thread 重建 host。
3. `entrypoints/host.py` 合并 builtin registry 与 `thread.extension_host.command_registry()`；CLI/TUI/RPC 不关心命令来自 builtin 还是 extension。
4. `/memory optimize` 和 `/memory eval generate` 从 `builtin.py` 迁入 `extensions/memory_evolution/extension.py` 注册；迁移阶段不要保留两个命令入口。
5. `tasks/models.py` 的 `TASK_KINDS` 允许由 system extension 注册，或者把 `memory_optimize` 保留为内置 kind 但实际 handler 由 ExtensionHost 拥有。推荐前者，避免 task kind 常量继续膨胀。

测试：

```text
tests/extensions/test_manifest.py
tests/extensions/test_extension_host.py
tests/extensions/test_memory_evolution_extension_registration.py
tests/entrypoints/test_extension_commands.py
tests/runtime/test_extension_context_lifecycle.py
```

验收：

1. `ExtensionHost.load_system_extensions()` 加载 `nanocode.memory_evolution` manifest。
2. activation 只注册贡献，不访问 `MemoryService`。
3. `/memory optimize` 仍可被 command registry 命中，但来源是 memory evolution extension。
4. `RuntimeThread` 切换/重建后旧 ctx 调用 fail loud。
5. 未开放 project/user extension 路径；不存在 `.nanocode/extensions` 自动加载。
6. 所有 extension diagnostics 可在启动或 task result 中定位到 extension id + handler。

### Phase 1 · RetrievalConfig 与 no-LLM configurable retrieval

新增：

```text
src/nanocode/memory/engines/simplemem/retrieval_config.py
```

定义：

```python
@dataclass(frozen=True)
class RetrievalConfig:
    schema_version: int = 1
    semantic_top_k: int = 25
    keyword_top_k: int = 5
    structured_top_k: int = 5
    max_context: int = 5
    fusion_mode: str = "rrf"  # rrf | semantic_only | keyword_only | structured_only
    weight_semantic: float = 1.0
    weight_keyword: float = 1.0
    weight_structured_person: float = 1.0
    weight_structured_entity: float = 1.0
    weight_timestamp: float = 0.6
    lexical_exact_boost: float = 0.0
    time_decay_half_life_days: float | None = None
```

边界：

1. 不放 LLM 字段。
2. 不放 answer prompt 字段。
3. `validate()` fail loud。
4. `from_dict()` 对未知字段 fail loud，不做 silent ignore。
5. `to_dict()` 稳定排序，便于 diff/history。

修改：

```text
src/nanocode/memory/engines/simplemem/config.py
src/nanocode/memory/engines/simplemem/engine.py
src/nanocode/memory/engines/simplemem/retriever.py
src/nanocode/memory/simplemem_backend.py
```

实现：

1. `SimpleMemConfig` 增加 `retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)`。
2. `SimpleMemBackend` 从 active store root 读取 `retrieval_config.json`，没有则用默认 config。
3. `Retriever` 不再接散落的 top_k 参数，改接 `RetrievalConfig`。
4. `retrieve_fast(query, limit)` 的实际返回数是 `min(limit, config.max_context)`。
5. RRF fusion 使用 config weights。
6. 保持 `retrieve_fast` 不调用 LLM 的测试。

测试：

```text
tests/memory/test_simplemem_retrieval_config.py
tests/memory/test_simplemem_engine.py
tests/memory/test_backend_config.py
```

验收：

1. `retrieve_fast()` LLM call count 仍为 0。
2. malformed `retrieval_config.json` 让 explicit simplemem backend 初始化失败或 service diagnostic，不降级 markdown。
3. 两个项目 hash store 使用不同 config，不互相污染。
4. 配置权重改变能改变排序。

### Phase 2 · Backend-aware eval generation

问题：当前 `build_eval_curator_message()` 只读 markdown memory files。SimpleMem backend 下应从 `MemoryService.backend.list/read` 或 engine entries 构造 eval 输入。

新增：

```text
src/nanocode/memory/eval_source.py
```

职责：

```text
build_eval_curator_message_from_backend(backend, *, max_entries, max_bytes)
```

实现：

1. markdown backend：保留当前 markdown file input 语义。
2. simplemem backend：列出 entries，格式化为 `## Memory: simplemem://<entry_id>` + content/keywords/time/persons。
3. off backend：返回 no memories sentinel。
4. 不让 eval curator 直接访问 store path。

修改：

```text
src/nanocode/runtime/spawn.py::spawn_memory_eval()
```

把：

```text
build_eval_curator_message()
```

替换为：

```text
host._memory_service.build_eval_curator_message()
```

或者：

```text
memory.eval_source.build_eval_curator_message(host._memory_service.backend)
```

`eval_store.MemoryEvalCandidate.source.memory_ref` 对 simplemem 使用 `simplemem://<entry_id>`。`prune_orphaned_evals` 必须 backend-aware，不能只检查 markdown filename。

验收：

1. simplemem backend 下 `/memory eval generate` 能从 indexed entries 生成 candidates。
2. host 仍统一填 `source.session_id`。
3. candidate 仍必须 human confirm。
4. model 不能通过 memory tool 添加/确认 eval。

### Phase 3 · Retrieval eval scorer

新增：

```text
src/nanocode/memory/retrieval_eval.py
```

数据模型：

```python
@dataclass(frozen=True)
class RetrievalEvalCase:
    id: str
    question: str
    answer: str
    evidence: tuple[str, ...]
    category: str
    source_ref: str

@dataclass(frozen=True)
class RetrievalEvalResult:
    case_id: str
    score: float
    answer_overlap: float
    evidence_overlap: float
    hit_count: int
    hit_refs: tuple[str, ...]
```

评分建议：

```text
score =
  0.55 * max token_f1(hit_content, answer)
  + 0.35 * max token_f1(hit_content, evidence_i)
  + 0.10 * top_rank_bonus
  - zero_retrieval_penalty
```

规则：

1. 默认不用 LLM judge。
2. confirmed eval 数不足则 optimize 不运行。
3. `answer` / `evidence` 为空的 candidate 不进入 eval。
4. 每个 case 输出 per-question report，供 diagnosis 使用。

后续可选：

1. `--judge llm` 仅用于离线 analysis，不作为唯一 promotion signal。
2. category-specific metrics 只影响 report，不影响第一版 promotion。

### Phase 4 · Host optimizer

新增：

```text
src/nanocode/memory/optimize.py
```

核心 API：

```python
def optimize_retrieval(
    engine: SimpleMemEngine,
    eval_cases: list[RetrievalEvalCase],
    current: RetrievalConfig,
    *,
    max_rounds: int,
    min_confirmed: int,
) -> OptimizationResult:
    ...
```

候选生成：

1. 从 current config 出发，生成 bounded candidates。
2. 每次只动 1-2 个字段。
3. 候选包括：
   - `semantic_top_k`: current ± 5，范围 `[0, 40]`
   - `keyword_top_k`: current ± 3，范围 `[0, 20]`
   - `structured_top_k`: current ± 3，范围 `[0, 15]`
   - `fusion_mode`: `rrf`, `semantic_only`, `keyword_only`, `structured_only`
   - weights: ±0.25，范围 `[0, 3]`
   - `max_context`: current ± 2，范围 `[3, 10]`
4. 对每轮 rejected move 记录 `(field, old, new)`，不重复尝试完全相同 move。

Promotion gate：

```text
sample_count >= min_confirmed
candidate mean_score >= baseline mean_score + min_delta
candidate zero_retrieval_count <= baseline zero_retrieval_count
candidate p10_score >= baseline p10_score - tolerance
holdout score not worse when eval_count >= 10
```

输出：

```text
OptimizationResult(
  promoted: bool,
  baseline_score: float,
  best_score: float,
  best_config: RetrievalConfig,
  rounds: list[RoundReport],
  rejected: list[RejectedMove],
  report_path: str,
)
```

Atomic persistence：

```text
{store_root}/retrieval_config.json
{store_root}/retrieval_config.<timestamp>.bak
{store_root}/optimize/history.jsonl
{store_root}/optimize/runs/<run_id>/summary.json
{store_root}/optimize/runs/<run_id>/cases.jsonl
```

不要复用 `maintenance.save_evolve_config()` 的旧路径。

### Phase 5 · `/memory optimize` wiring

修改：

```text
src/nanocode/extensions/memory_evolution/commands.py
src/nanocode/extensions/memory_evolution/tasks.py
src/nanocode/extensions/memory_evolution/reports.py
src/nanocode/extensions/host.py
src/nanocode/runtime/facade.py
src/nanocode/runtime/spawn.py
```

行为：

1. `/memory optimize` command handler 只做参数解析和 `TaskManager.create_task(kind="memory_optimize")`。
2. task execution 由 `ExtensionHost.run_task("memory_optimize", payload)` 分发到 `memory_evolution.tasks.run_memory_optimize_task()`。
3. 如果无 `MemoryService`：task completed with diagnostic。
4. 如果 active backend != simplemem：task completed with explicit unsupported backend。
5. 如果 simplemem backend 不可用：task failed/unavailable with explicit diagnostic。
6. 如果 confirmed eval 数不足：task completed no-op，写明需要多少、当前多少。
7. 成功 run optimizer 后，把 summary 写入 task result path。
8. promotion 成功后 task summary 显示 config path 和 delta。
9. promotion 失败后 task summary 显示 baseline/best/no promotion reason。

命令文案：

```text
/memory optimize
  Run host-owned retrieval optimization on confirmed memory eval candidates
```

不要说 “Run EvolveMem” 以免暗示 upstream full system 已接。

收敛旧入口：

1. `entrypoints/commands/builtin.py` 删除 `/memory optimize` 的 builtin 注册。
2. `RuntimeThread.spawn_memory_optimize()` 可删除；如果 command dispatcher 仍需要统一方法，则只做薄转发到 `extension_host.run_command("/memory optimize")`，不要保留业务逻辑。
3. `runtime/spawn.py::run_memory_optimize()` 删除 unavailable stub；真实 handler 在 extension task。
4. tests 中仍覆盖命令命中和 task lifecycle，但断言 command source 是 `extension:nanocode.memory_evolution`。

### Phase 6 · Optional multi-agent diagnosis

新增 reserved agent type：

```text
MEMORY_RETRIEVAL_DIAGNOSIS_TYPE = "memory-retrieval-diagnostician"
```

修改：

```text
src/nanocode/extensions/memory_evolution/agents.py
src/nanocode/extensions/memory_evolution/models.py
src/nanocode/extensions/memory_evolution/tasks.py
src/nanocode/extensions/registry.py
src/nanocode/subagents/prompts.py
src/nanocode/agents/registry.py
```

Prompt 约束：

```text
You are a retrieval diagnosis agent.
You receive:
  - current RetrievalConfig
  - aggregate metrics
  - worst failure cases
  - rejected move history
You must output strict JSON:
{
  "root_causes": [...],
  "parameter_suggestions": {...},
  "reasoning": "...",
  "risk": "..."
}
Allowed fields: ...
Do not propose writes, tools, code changes, memory edits, or eval confirmation.
```

权限：

1. 由 `nanocode.memory_evolution` system extension 注册 reserved hidden system agent。
2. tools = `[]` by default；如果必须读 report，则 host 直接把 report summary 放 prompt，不给 read_file。
3. background=True，confirm_fn auto-deny。
4. max_turns=1。
5. 无 MemoryService。
6. 不允许 spawn child。
7. 模型选择走 `ExtensionModelRouter.resolve("memory_diagnosis")`，不是写死在 prompt。

Host 处理：

1. Parse JSON。
2. Reject unknown fields。
3. Clamp values。
4. Generate candidate configs。
5. Re-run deterministic eval。
6. Promotion gate。

### Phase 7 · 删除/收敛旧残留

删除或改名：

```text
src/nanocode/memory/maintenance.py::load_evolve_config
src/nanocode/memory/maintenance.py::save_evolve_config
src/nanocode/memory/maintenance.py::rollback_evolve_config
src/nanocode/memory/maintenance.py::evolve_config_path
```

如果仍需要对外命令 `rollback`，改成：

```text
src/nanocode/memory/retrieval_config_store.py::rollback_retrieval_config()
```

不要保留两个 config truth source。

## 8. Multi-agent 接入细化

### 8.1 推荐的 agent 拓扑

```text
Human
  confirms eval candidates
  can approve future review queue if enabled

Host / RuntimeThread
  owns MemoryService
  owns task lifecycle
  owns optimizer
  owns promotion gate
  writes retrieval_config.json

Eval Curator Agent
  input: backend-formatted memory entries
  output: pending QA candidates
  cannot confirm
  cannot write memory

Failure Diagnostician Agent
  input: optimize report summary
  output: candidate parameter suggestions
  cannot write config
  cannot judge final promotion

Meta Analyst Agent
  input: optimize history
  output: proposed future action-space extensions
  writes only report text via host, never code/config
```

### 8.2 为什么不做“多个优化 agent 竞赛然后投票”

不推荐让多个 agent 各自优化、互评、投票。原因：

1. eval set 小时 agent 很容易过拟合。
2. agent 互评仍是 LLM self-validation。
3. 多 agent token cost 高，而 promotion 的关键不是生成更多意见，是确定性 replay。
4. nanocode 已经有 host task/task_output，最自然的边界是 host 作为 referee。

可以并行的是 candidate evaluation，因为它是 deterministic Python，不是多 agent。

## 9. 验收标准

### 9.1 边界验收

1. `import nanocode.agent.core` 不 import `nanocode.memory`。
2. `import nanocode.memory.engines.simplemem` 不 import `nanocode.runtime` / `nanocode.session`。
3. `import nanocode.extensions` 不加载 project/user extension，不执行 optimize worker。
4. `ExtensionHost.activate_all()` 只登记贡献，不访问 `MemoryService`。
5. `ExtensionContext.session` 是 `ReadOnlySessionView`，无 session mutation API。
6. session replacement 后旧 extension ctx fail loud。
7. `retrieve_fast()` 单测证明 LLM call count 为 0。
8. `/memory optimize` 在 sub-agent 内不可用或 host-only 拒绝。
9. optimization worker 不暴露 memory tool、MCP、network、shell。
10. explicit simplemem config failure 不 fallback markdown。

### 9.2 功能验收

1. simplemem backend 下 `/memory eval generate` 能基于 SimpleMem entries 生成 pending candidates。
2. `/memory eval confirm <id>` 后 confirmed set 可被 optimizer 读取。
3. confirmed 数不足时 `/memory optimize` 不运行 candidate loop，输出明确原因。
4. confirmed 数足够时 optimizer 生成 baseline/candidate report。
5. candidate 提升达到 gate 时，原子写 `retrieval_config.json`。
6. candidate 未通过时，不改 live config。
7. 重启后 `SimpleMemBackend` 读取 promoted config。
8. malformed config 失败可见，不静默忽略。

### 9.3 测试建议

新增测试：

```text
tests/extensions/test_manifest.py
tests/extensions/test_extension_host.py
tests/extensions/test_memory_evolution_extension_registration.py
tests/runtime/test_extension_context_lifecycle.py
tests/memory/test_retrieval_config.py
tests/memory/test_retrieval_eval.py
tests/memory/test_memory_optimize.py
tests/agent/test_memory_optimize.py
tests/entrypoints/test_memory_optimize_command.py
tests/subagents/test_memory_diagnostician_config.py
```

重点断言：

1. extension import/activation 不触发 host business。
2. extension commands 与 builtin registry 合并后 most-specific-first 仍成立。
3. no-LLM hot path。
4. config load/save atomic。
5. candidate generator bounded。
6. promotion gate prevents regression。
7. backend-aware eval source works for markdown/simplemem/off。
8. reserved diagnosis agent cannot be overridden by project agent definitions。

## 10. 推荐实施顺序

1. 实现 `src/nanocode/extensions/*` 的 Pi-aligned system ExtensionHost/API/manifest/context。
2. 把 `/memory optimize`、`/memory eval generate` 迁入 `nanocode.memory_evolution` system extension 注册；删除重复 builtin 入口。
3. `docs/22` 落地后，删/隔离 `maintenance.py` 里旧 `evolve_config` truth source。
4. 实现 `RetrievalConfig` + config store。
5. 改 `Retriever` 为 config-driven no-LLM。
6. 实现 backend-aware eval source，修正 simplemem 下 `/memory eval generate`。
7. 实现 deterministic `memory/optimize.py`。
8. 接 `/memory optimize` task handler 到 ExtensionHost。
9. 补单测。
10. 运行：

```bash
pytest -q tests/extensions/test_manifest.py \
  tests/extensions/test_extension_host.py \
  tests/extensions/test_memory_evolution_extension_registration.py \
  tests/runtime/test_extension_context_lifecycle.py \
  tests/memory/test_simplemem_engine.py \
  tests/memory/test_eval_store.py \
  tests/memory/test_retrieval_config.py \
  tests/memory/test_retrieval_eval.py \
  tests/memory/test_memory_optimize.py \
  tests/agent/test_memory_optimize.py \
  tests/entrypoints/test_memory_optimize_command.py
```

11. 第二轮再接 `memory-retrieval-diagnostician`。

## 11. 最终取舍

EvolveMem 在 nanocode 里的正确身份是一个 **memory evolution system extension**：它不是 `SimpleMemEngine` 内部模块，不是普通用户可覆盖的任意代码插件，也不是单个 sub-agent；它通过 Pi-style factory/API/runner/context 形态注册，由 runtime/task/MemoryService 执行边界约束，最终表现为 host-owned 离线优化能力。

推荐终态：

```text
MemoryEvolutionExtension
  command / task / agent / model policy registration

SimpleMemEngine
  retrieval algorithm only

MemoryService
  runtime boundary + config loading

RetrievalOptimizer
  host-only offline optimizer

Eval Curator / Diagnosis Agent
  proposal-only system agents

Promotion Gate
  deterministic host code
```

这既吸收了 EvolveMem 的核心贡献，也保留 nanocode 已经建立的 Pi/Codex 式边界：session tree 是唯一上下文事实源，runtime/host 拥有 lifecycle，engine 不拥有 agent/tool/permission。
