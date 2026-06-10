# nanocode Trajectory 改造报告：Harness 可观测性与 Agentic RL 数据底座

日期：2026-06-09

范围：nanocode 当前 `wire.jsonl` / `trace` / session v2 机制，以及面向长任务、Agent Harness 可观测性、agentic RL 研究数据的 Trajectory mode 改造。

目标：在不引入第二套事实源、不复活 session 根 `events.jsonl` 的前提下，为 nanocode 增加一个显式开启的长任务轨迹采集模式，使其能支撑生产级 Harness 观测、复盘、评估与后续轨迹数据导出。

## 结论

Trajectory mode 可行，而且应作为 nanocode 下一阶段可观测性的核心增量。但它不能只是“多写日志”，而应被定义为：

```text
wire.jsonl    always-on 原始事实账本
trace         开发调试 lane
trajectory    显式开启的长任务行为轨迹层
eval/reward   后处理或在线评估层
```

当前 `wire.jsonl` 已经提供了良好的事件基础：按 agent 分文件、append-only、带 `session_id` / `seq` / `ts` / `agent_id` / `turn_id` / `parent_id` 等 envelope，可用于审计、基础 replay 和故障定位。但它距离 Harness 文章所描述的生产级可观测性还有明显差距：缺少任务目标演化、上下文引用、状态差异、质量评估、风险分级、失败归因、trajectory dataset 投影等。

因此建议新增：

```bash
nanocode --trajectory
nanocode --trajectory-level summary
nanocode --trajectory-level full
```

环境变量：

```bash
NANOCODE_TRAJECTORY=1
NANOCODE_TRAJECTORY_LEVEL=summary|full
```

默认不开启 Trajectory，避免所有普通会话都记录重 payload、敏感内容和巨大工具结果。长任务、生产诊断、研究采集时显式开启。

## 当前状态

nanocode 当前有三类容易混淆的记录机制：

| 机制 | 路径 | 默认 | 角色 |
| --- | --- | --- | --- |
| flat JSON session | `~/.nanocode/sessions/<session_id>.json` | 兼容旧会话 | 最早期 snapshot 存储 |
| session v2 snapshot | `~/.nanocode/sessions/<session_id>/state.json`, `main/messages.json`, `agents/<id>/messages.json` | 有条件写入 | resume/cache，保存模型实际消息状态 |
| per-agent wire | `~/.nanocode/sessions/<session_id>/agents/<id>/wire.jsonl` | always-on | 当前原始事件账本 |
| debug trace | `./.nanocode/traces/<session_id>.jsonl` | `--trace` 才开启 | 开发调试、sandbox 子 trace |

需要明确：

1. **不再新增 session 根 `events.jsonl`**。早期 Pi-style 方案里的 session-level event store 已被否决，统一时间线由读侧 merge per-agent `wire.jsonl` 得到。
2. **`wire.jsonl` 是当前唯一应扩展的事件账本**。Trajectory 不应该新建一条 durable event lane，而应通过 wire 增强字段和导出投影实现。
3. **snapshot 仍然重要**。在 compaction / snip / budget / microcompact 全部事件化之前，resume 不能只依赖 append-only 事件重建，仍需要 `messages.json` 兜底。

## 与 Harness 可观测性的差距

参考 Agent Harness 可观测性的要求，生产级观测不只是记录 prompt 和 tool call，而是要覆盖完整任务链路：

```text
goal -> plan -> context -> model decision -> tool action -> state change -> risk -> eval -> improvement
```

当前 `wire.jsonl` 已覆盖一部分基础事件：

- session / turn 边界
- user message
- LLM request / response
- assistant message / tool uses
- tool call / tool result
- permission decision
- compaction / budget / turn_end
- token usage和部分错误信息

缺口主要在这些方面：

| 维度 | 当前状态 | 缺口 |
| --- | --- | --- |
| Task | 有 session/turn，但缺 task-level normalized goal | 难以跨会话统计任务类型和目标漂移 |
| Step | 有事件，但不稳定映射为 RL step | 缺明确 `step_id` / `step_type` / action-observation 边界 |
| Context | `llm_request.messages` 可记录完整上下文 | 缺 context refs、token breakdown、compaction 状态摘要 |
| State | 工具结果有文本 | 缺 state_before/state_after、文件 diff、测试结果结构化 |
| Risk | 有 permission decision | 缺 risk_level、approval rationale、危险操作归因 |
| Eval | 基本没有 | 缺 final_eval、step_eval、reward、failure_reason |
| Metrics | 有 token/cost 基础 | 缺 latency 分解、retry、工具耗时、长任务健康指标 |
| Export | `nanocode trace --wire` 可看时间线 | 缺 trajectory dataset / metrics bundle 导出 |

这意味着：当前 wire 对“审计和调试”已经有价值，但对“生产 Harness 可观测性”和“agentic RL 数据”还只是基础设施，不是完整答案。

## Trajectory 的设计定位

Trajectory mode 应解决三类问题：

1. **长任务复盘**：能回答“agent 为什么这么做、在哪一步跑偏、哪个工具或上下文导致失败”。
2. **生产观测**：能汇总成本、延迟、失败率、审批风险、上下文压力、工具错误、任务完成质量。
3. **研究数据**：能把原始事件投影成 `observation -> action -> result -> next_state -> reward` 的 step dataset。

它不应解决：

1. 不应替代 `wire.jsonl` 作为事实源。
2. 不应替代 `messages.json` 作为迁移期 resume snapshot。
3. 不应把所有普通会话默认变成完整 prompt/tool/result 存档。
4. 不应把敏感内容默认写入可长期保留的数据包。

推荐语义：

```text
off      默认，只写轻量 wire
summary  长任务推荐，记录摘要、引用、指标、状态差异
full     本地复盘，记录完整 messages、tool args/result、必要原文
```

后续如确有需要，可再加 `research` 导出格式，但不建议第一阶段把 runtime 采集层和 RL dataset 格式强耦合。

## 目标 Schema

### Task-level 字段

Trajectory 开启时，session / turn / final 相关事件应尽量补齐：

```json
{
  "trajectory": true,
  "trajectory_id": "traj_<session_id>",
  "trajectory_level": "summary",
  "task_id": "task_<session_id>",
  "user_goal": "...",
  "normalized_goal": "...",
  "agent_version": "...",
  "model_version": "...",
  "policy_version": "...",
  "start_time": "...",
  "end_time": "...",
  "final_status": "completed|failed|cancelled|timeout",
  "total_tokens": 12345,
  "total_cost": 0.12,
  "final_eval": null,
  "failure_reason": null
}
```

这些字段不要求都在一个事件里一次性出现。更合理的方式是按生命周期增量写入：

- `session_start` / `turn_start` 写 `trajectory_id`、`user_goal`
- `llm_response` / `tool_result` 写 token、latency、status
- `turn_end` / `session_end` 写 total、final_status、failure_reason、final_eval

### Step-level 字段

每个可训练/可复盘的关键动作应能投影为 step：

```json
{
  "trajectory_id": "traj_<session_id>",
  "step_id": "step_<agent_id>_<seq>",
  "parent_step_id": "step_<agent_id>_<prev_seq>",
  "turn_id": "turn_main_12",
  "agent_id": "main",
  "step_type": "llm_decision|tool_action|tool_observation|approval|compaction|final",
  "current_goal": "...",
  "observation_summary": "...",
  "action": {
    "type": "tool_call",
    "tool": "read_file",
    "args_summary": "..."
  },
  "result_summary": "...",
  "next_state_summary": "...",
  "latency_ms": 850,
  "input_tokens": 1000,
  "output_tokens": 200,
  "cost": 0.01,
  "risk_level": "low|medium|high",
  "eval_result": null,
  "reward": null,
  "done": false
}
```

这里的 `step` 是读侧投影概念，不一定要求盘上 wire 事件直接使用该嵌套结构。盘上仍建议保持当前 flat-additive 风格，避免破坏 `trace/report.py` 对顶层字段的读取。

## wire 增强字段建议

为了兼容当前代码，第一阶段推荐在现有 flat event 上增量加字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `trajectory` | bool | 是否属于 trajectory 采集 |
| `trajectory_id` | str | 一次 trajectory 的稳定 id，默认 `traj_<session_id>` |
| `trajectory_level` | str | `summary` 或 `full` |
| `step_id` | str | 可选，便于直接索引 step |
| `step_type` | str | `llm_decision` / `tool_action` / `tool_observation` 等 |
| `current_goal` | str | 当前局部目标或 planner 摘要 |
| `observation_summary` | str | 当前模型可见状态摘要 |
| `model_input_summary` | str | prompt/messages 摘要 |
| `model_output_summary` | str | assistant 输出摘要 |
| `tool_args_summary` | str | 工具参数摘要 |
| `tool_result_summary` | str | 工具结果摘要 |
| `state_before_ref` | str | 指向状态快照或摘要 artifact |
| `state_after_ref` | str | 指向状态快照或摘要 artifact |
| `state_diff_summary` | str | 文件/任务/上下文变化摘要 |
| `files_touched` | list[str] | 本 step 触达文件 |
| `tests_run` | list[dict] | 测试命令、退出码、摘要 |
| `latency_ms` | int | step 或请求耗时 |
| `risk_level` | str | 风险分级 |
| `eval_result` | dict | 评估结果，可为空 |
| `reward` | float | 后处理或在线 reward，可为空 |

### summary 与 full 的差异

`summary` 级别：

- 写摘要、引用、metrics、状态变化
- 默认不写完整 `llm_request.messages`
- 默认不写完整巨大 `tool_result.result`
- 工具参数只写脱敏/截断摘要，必要时写 artifact ref

`full` 级别：

- 写完整 `llm_request.messages`
- 写完整 tool args/result，或完整内容 artifact ref
- 保留更高复盘能力，但需要明确本地隐私风险

建议不要在 `summary` 中彻底丢失可追溯性。更好的方式是：

```text
summary event 写摘要 + hash + artifact_ref
full event 写完整 payload 或完整 artifact_ref
```

## Agentic RL 数据投影

Trajectory 采集层不应直接等同于 RL dataset。更合理的分层是：

```text
wire events -> trajectory projection -> eval/reward augmentation -> dataset export
```

推荐导出格式：

```text
trajectory/
  metadata.json
  steps.jsonl
  metrics.json
  artifacts.json
  prompts.json
  tools.json
  evals.jsonl
```

`steps.jsonl` 是 RL / SFT / behavior cloning 最常用入口：

```json
{
  "trajectory_id": "traj_abc",
  "episode_id": "sess_abc",
  "step_id": "step_main_42",
  "observation": "...",
  "state_summary": "...",
  "action_type": "tool_call",
  "action": {"tool": "edit_file", "args": {"path": "..."}},
  "result": "...",
  "next_state_summary": "...",
  "reward": null,
  "done": false,
  "cost": {"tokens": 1200, "latency_ms": 850},
  "metadata": {
    "agent_id": "main",
    "turn_id": "turn_main_5",
    "risk_level": "medium"
  }
}
```

对 agentic RL 来说，最关键的不是保存所有原文，而是稳定表达：

1. agent 当时看到了什么：`observation`
2. agent 做了什么：`action`
3. 环境返回什么：`result`
4. 状态如何变化：`next_state_summary` / `state_diff`
5. 成本与风险如何：`tokens` / `latency` / `risk`
6. 结果好不好：`eval_result` / `reward`

第一阶段可以允许 `reward=null`，先支持 replay、diagnosis、SFT/behavior cloning。后续通过 eval pipeline 回填 reward。

## Eval / Reward 层

Trajectory 要满足 agentic RL，必须规划 eval/reward，否则只能算日志或演示级 replay。

建议分三类评估：

| 类型 | 触发时机 | 输出 |
| --- | --- | --- |
| online heuristics | 每个 tool_result / turn_end | exit code、测试是否通过、文件是否存在、权限是否被拒 |
| offline evaluator | trajectory export 后 | task success、step usefulness、failure attribution |
| human feedback | 用户复盘或标注 | accepted/rejected、rating、correction、reward |

第一阶段可落地的低成本 reward 信号：

- 命令退出码
- 测试通过/失败数量
- tool error / timeout
- permission denied
- 是否完成 final answer
- 是否触达用户指定文件
- 是否产生 expected artifact
- 是否发生 compaction / context overflow
- 用户是否继续追问“没解决”

这些信号不完美，但足以支撑失败聚类、轨迹筛选、行为克隆样本清洗和基础 reward modeling。

## 模块影响

### `src/nanocode/trace/config.py`

新增 trajectory 配置：

```python
def trajectory_enabled(flag: bool = False) -> bool: ...
def trajectory_level(value: str | None = None) -> str: ...
```

读取：

- CLI flag
- `NANOCODE_TRAJECTORY`
- `NANOCODE_TRAJECTORY_LEVEL`

合法 level：

- `summary`
- `full`

### `src/nanocode/entrypoints/cli.py`

新增参数：

```text
--trajectory
--trajectory-level {summary,full}
```

传入 `Agent(...)`。

CLI help 中要明确：

- `--trace` 是 debug trace
- `--trajectory` 是长任务轨迹采集
- `--trajectory-level full` 可能记录敏感上下文和工具结果

### `src/nanocode/agent/engine.py`

`Agent.__init__` 增加：

```python
trajectory_enabled: bool = False
trajectory_level: str = "summary"
```

`_build_tracer()` 创建 `Tracer` 时传入 trajectory 配置。

`chat()` / subagent 创建路径需要继承 trajectory 配置，使前台和后台子 agent 的 wire 也带同一个 `trajectory_id`，但仍写各自的 `agents/<id>/wire.jsonl`。

### `src/nanocode/trace/tracer.py`

`Tracer` 增加：

```python
trajectory_enabled: bool
trajectory_level: str
trajectory_id: str | None
```

`emit()` 在 envelope 中注入：

```python
if trajectory_enabled:
    event["trajectory"] = True
    event["trajectory_id"] = self.trajectory_id
    event["trajectory_level"] = self.trajectory_level
```

注意：不要在 `Tracer.emit()` 里做复杂摘要、读取文件、计算 diff。`Tracer` 应保持轻量，复杂语义在调用点或 exporter 做。

### `src/nanocode/agent/openai_backend.py` 与 `anthropic_backend.py`

按 level 控制重字段：

- `summary`：写 message count、token count、摘要、hash/ref
- `full`：写完整 `messages`

工具事件：

- `tool_call`：默认写 `tool`、`tool_use_id`、参数摘要；full 写完整 args
- `tool_result`：默认写 status、chars、摘要、artifact ref；full 写完整 result 或完整 artifact ref

当前如果直接移除默认 `llm_request.messages`，会影响事件 rebuild 路径。必须确保 `SessionContextBuilder` 在缺少完整 messages 时可靠 fallback 到 snapshot。

### `src/nanocode/agent/context_builder.py`

需要明确 rebuild 策略：

1. 若 wire 中存在完整 `llm_request.messages` 且 faithful rebuild 条件满足，可用事件重建。
2. 若 trajectory summary 不含完整 messages，必须 fallback 到 `messages.json`。
3. 不能因为开启/关闭 trajectory 改变 resume 的用户可见语义。

### `src/nanocode/entrypoints/trace_cmd.py`

短期可扩展现有命令：

```bash
nanocode trace --wire --trajectory <session_id>
```

但更推荐后续新增独立命令组：

```bash
nanocode trajectory list
nanocode trajectory show <session_id>
nanocode trajectory export <session_id>
```

### 新增 `src/nanocode/trajectory/`

建议新增包：

```text
src/nanocode/trajectory/
  __init__.py
  project.py        # wire -> step projection
  export.py         # bundle export
  metrics.py        # cost/latency/failure/risk metrics
  schema.py         # TypedDict/dataclass for exported views
  redaction.py      # basic redaction/truncation helpers
```

第一阶段可以只做 `project.py` 和 `export.py`。

## 实施计划

### P0：语义和配置

目标：让用户能显式开启 Trajectory，但不改变现有行为。

改动：

- 增加 `--trajectory`
- 增加 `--trajectory-level`
- 增加 `NANOCODE_TRAJECTORY`
- 增加 `NANOCODE_TRAJECTORY_LEVEL`
- `Tracer.emit()` 注入 trajectory envelope

验收：

- 默认运行仍只写当前 wire 字段
- `--trajectory` 后 wire 事件带 `trajectory=true`
- 子 agent 继承 trajectory id/level

### P1：summary/full payload 控制

目标：避免默认 wire 在长任务中无限膨胀。

改动：

- `llm_request` 在 summary 下不写完整 messages，改写摘要/计数/ref
- `tool_result` 在 summary 下不写巨大 result，改写摘要/chars/ref
- full 下保留完整 payload
- rebuild 缺 full payload 时 fallback snapshot

验收：

- 普通 wire 文件显著变小
- full 仍可用于本地完整复盘
- resume 不丢上下文

### P2：Trajectory projection

目标：把 wire 投影成可分析 step。

改动：

- 新增 `trajectory.project`
- 将 `llm_request` / `assistant_message` / `tool_call` / `tool_result` 配对成 step
- 输出 `steps.jsonl`
- 输出 `metrics.json`

验收：

- 一个会话能导出稳定 step 序列
- 每个 tool action 有 observation/action/result/cost/status
- malformed/legacy wire 行不会导致导出崩溃

### P3：Harness metrics

目标：满足生产观测的最小指标面。

指标：

- total turns
- total tool calls
- tool failure rate
- permission deny rate
- total/input/output tokens
- estimated cost
- model latency
- tool latency
- compaction count
- timeout/cancel/error count
- high-risk action count
- files touched
- tests run/pass/fail

验收：

- `nanocode trajectory export` 产出 `metrics.json`
- 支持按 agent / turn / tool 聚合
- 能定位失败最多的工具和最高成本 step

### P4：Eval / reward augmentation

目标：让 trajectory 能进入 agentic RL 数据准备流程。

改动：

- 支持 offline eval 回填 `evals.jsonl`
- 支持 step-level `reward`
- 支持 failure attribution
- 支持 human feedback artifact

验收：

- 同一 trajectory 可先导出 reward=null，再二次 augment
- reward 不污染原始 wire
- dataset export 可过滤失败/高风险/低质量样本

## 兼容性与迁移

### 对旧 session

旧 flat JSON 和旧 v2 session 不迁移。Trajectory 只对开启后的新事件生效。

读侧应容忍：

- 没有 `trajectory` 字段
- legacy flat wire 行
- 缺少 `id` / `parent_id`
- 缺少 `messages` full payload
- malformed JSONL 行

### 对 `wire.jsonl` schema

保持 flat-additive。新增字段只加不改名、不嵌套 payload 到 `data`。

原因：

- `trace/report.py` 当前读顶层字段
- `events.models.SessionEvent.from_wire` 已按 envelope/data 派生视图工作
- flat-additive 对老 reader 更友好

### 对 resume

Trajectory 不能改变 resume 语义。

特别注意：

- summary level 可能不含完整 `llm_request.messages`
- `SessionContextBuilder.rebuild_messages()` 必须在缺 full payload 时返回失败/空结果
- `Agent.restore_session()` 必须 fallback 到 v2 `messages.json`

## 隐私与安全

Trajectory full 会记录大量敏感信息：

- 完整 prompt/messages
- 文件内容
- shell 输出
- 环境信息
- 工具参数
- 错误堆栈

因此：

1. 默认不开启。
2. 默认 level 为 `summary`。
3. CLI help 必须提示 full 的敏感性。
4. export 应支持 redaction。
5. summary 中优先写摘要、hash、artifact ref。
6. 后续如支持远端上传，必须单独设计 consent 和 redaction，不应复用本地开关。

## 风险

| 风险 | 等级 | 控制方式 |
| --- | --- | --- |
| wire 膨胀过快 | 高 | summary 默认不写 full payload；大结果落 artifact ref |
| 敏感信息泄露 | 高 | 默认 off；full 明确 opt-in；redaction |
| resume 行为回归 | 高 | rebuild 缺 full payload 必须 fallback snapshot |
| schema 过早冻结 | 中 | flat-additive；export schema 可版本化 |
| eval/reward 质量差 | 中 | reward 可空；先支持过滤/标注，不强行自动 RL |
| 和 `--trace` 语义混淆 | 中 | 文档和 CLI help 明确三层：wire/trace/trajectory |
| 子 agent 轨迹断裂 | 中 | 继承 trajectory_id，按 agent_id 分文件，读侧 merge |

## 建议的第一刀

第一刀不要做完整平台，只做最小闭环：

1. `--trajectory` / `--trajectory-level`
2. `Tracer` 注入 trajectory envelope
3. 子 agent 继承 trajectory 配置
4. summary/full 控制 `llm_request.messages` 和 `tool_result.result`
5. `SessionContextBuilder` fallback 测试
6. 文档更新 `docs/08-trace.md`，明确 wire/trace/trajectory

建议先不做：

- 新的 session 根 `events.jsonl`
- OTel 后端
- 在线 reward model
- 复杂 UI
- 远端上传

## 最终目标形态

理想状态下，nanocode 的可观测性和研究数据流应变成：

```text
Agent runtime
  -> Tracer.emit(flat-additive event)
  -> agents/<id>/wire.jsonl
  -> reader.merge_session_events()
  -> trajectory.project()
  -> trajectory bundle
       metadata.json
       steps.jsonl
       metrics.json
       artifacts.json
       evals.jsonl
  -> diagnosis / dashboard / offline eval / RL dataset
```

这条路径把当前已有的 wire 资产最大化利用，同时避免了 session-level `events.jsonl` 带来的并发写入和事实源分裂。

## 一句话原则

Trajectory mode 应该是 **基于现有 per-agent `wire.jsonl` 的显式长任务轨迹采集与导出层**：默认轻量、开启后可观测、导出后可训练，原始事实源仍保持唯一。
