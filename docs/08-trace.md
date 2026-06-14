# Trace —— agent 轨迹记录

> 源码位置：`src/nanocode/trace/`
> 关键文件：`tracer.py`（Tracer/NullTracer）、`sinks.py`（Sink 协议/JsonlSink）、`config.py`（开关/输出）
> 开关：默认关闭；`--trace` 或 `NANOCODE_TRACE=1` 开启

## 它解决什么
低侵入地记录一次运行的完整轨迹（trajectory）用于离线分析：用户消息、LLM 请求（含消息快照）、assistant 文本/thinking/工具调用、工具结果、权限决策、压缩、预算、token 用量。默认关闭，开启时零数据丢失（逐条 flush）。

## 它如何工作
Agent 持一个 `tracer`：关闭时是 `NullTracer`（`emit` 为零开销 no-op，不创建任何文件）；开启时是 `Tracer`，在循环约 10 个节点调用 `tracer.emit(type, **fields)`。事件统一附 `v/ts/session_id/parent_session_id/seq/type`，经可插拔 `Sink` 写出。默认 `JsonlSink` → `./.nanocode/traces/<session_id>.jsonl`（一行一事件）。`trace/` 是叶子包，不依赖核心；核心只多约 10 行 emit。instrumentation 吞掉一切异常，绝不影响 agent。

## 子 Agent 嵌套
子 Agent 由父传 `trace_parent=self.tracer`，在构造时 `trace_parent.child(session_id)` 派生：共享同一组 sink、带 `parent_session_id`。据此可重建嵌套调用树。

## 事件类型（schema v1）
`session_start, user_message, llm_request, assistant_message, llm_response, permission_decision, tool_call, tool_result, compaction, budget_exceeded, turn_end, session_end`

## 如何加自定义后端（sink）
实现 `Sink` 协议（`write(event: dict)` / `close()`），构造时传入：
```python
from nanocode.trace import make_tracer
tracer = make_tracer(session_id, enabled=True, sinks=[MyOTelSink(), MySqliteSink()])
```
或在 `config.build_default_sinks` 中追加。事件是普通 dict、与 schema 解耦，新增事件类型无需改 sink。

## 查看/汇总 trace（`nanocode trace`）

只读子命令，渲染 `./.nanocode/traces/` 下的 JSONL：

- `nanocode trace` —— 列出所有会话（id / 时间 / 事件数 / 估算费用 / 首条消息）。
- `nanocode trace <id>` —— 打印该会话紧凑时间线（每事件一行，子 Agent 缩进嵌套）。
  - id 支持前缀匹配与 `latest`。
- `nanocode trace <id> --full` —— 展开消息/工具结果全文。
- `nanocode trace <id> --summary` —— 汇总（轮数、各工具调用次数、token、估算费用、时长、子 Agent 数、budget/deny 次数）。

实现：`src/nanocode/trace/report.py`（纯函数渲染）+ `src/nanocode/entrypoints/trace_cmd.py`（命令处理器）。

## 三条记录线（wire / --trace / --trajectory）

nanocode 有三条彼此独立、用途不同的记录线，务必区分：

| 线 | 开关 | 角色 | 物理位置 |
| --- | --- | --- | --- |
| **wire** | 常开（always-on） | 每个 agent 的**持久执行事实**（durable event log）；resume / fork 的权威 | `agents/<id>/wire.jsonl`（per-agent，读侧由 `events.reader.merge_session_events` 跨 agent 合并为 entry tree） |
| **--trace** | opt-in（`--trace` / `NANOCODE_TRACE=1`） | debug lane：一次运行的完整调试轨迹，逐条 flush，离线排障 | `./.nanocode/traces/<session>.jsonl` |
| **--trajectory** | opt-in（`--trajectory` / `NANOCODE_TRAJECTORY=1`） | 长任务 trajectory 采集：把常开 wire **投影**为可分析 / agentic-RL 的 trajectory | 派生 bundle 落 `session_root/trajectory/`（详见下） |

### 硬架构边界（用户强制 —— 绝不可违反）

- **wire = 执行事实 / 持久事件日志。**
  - trajectory level=**FULL**：wire 保留完整 payload（可用于 event-tree rebuild）。
  - trajectory level=**SUMMARY**：wire 丢弃重型 payload（messages / 大 result），event-tree rebuild **退化为 snapshot**——这是**预期行为**，已由 `SessionContextBuilder` 处理。
- **trajectory（`src/nanocode/trajectory/`）= 派生（DERIVED）投影**，用于分析 / RL。
  - 它**绝不**驱动 runtime，**绝不**用于 resume / fork。
  - 任何 runtime 模块（`agent/engine.py`、`agent/anthropic_backend.py`、`agent/openai_backend.py`、`agent/context_builder.py`、`session/agent.py`、`trace/tracer.py`、`trace/redaction.py`）**绝不**得 `import nanocode.trajectory`。
  - trajectory 包**只读** merged wire，**绝不**写回 wire。
- **metrics / evals = 派生标签。** 绝不污染 wire：runtime 的 emit 路径**绝不**把 reward / eval_result 写进 wire；reward / eval 只活在 `metrics.json` / `evals.jsonl`。

### 开关 / 环境变量

- `--trajectory` / `NANOCODE_TRAJECTORY=1`：开启 trajectory 采集（默认**关闭**）。
- `--trajectory-level {summary,full}` / `NANOCODE_TRAJECTORY_LEVEL`：默认 **summary**；非法 / 缺省退回 summary。
  - **summary**（保守默认）：丢重型 payload，只留 hash + 摘要 + 长度。
  - **full**：保留完整 prompts / messages / tool results——**隐私敏感**（可能含密钥）。

### summary vs full —— wire 上的 payload 行为

trajectory 开启时，每条事件 flat-additive 地补 `trajectory=True` / `trajectory_id` / `trajectory_level`：

- **FULL（或 trajectory 关闭）**：payload 原样不动，与今天 byte-identical；可用于 event-tree rebuild。
- **SUMMARY**：`trace/redaction.apply_summary_shaping` 就地整形，丢重型字段：
  - `llm_request.messages` → pop，补 `messages_chars` + `messages_hash`（保留 `message_count`）。
  - `tool_result.result` → pop，补 `result_summary`（截断）+ `result_hash`（保留 `chars`）。
  - 重型 payload 既已丢，event-tree rebuild **退化为 snapshot**（intended，由 `SessionContextBuilder` 兜底）。

> **隐私要点（务必读）**：
> - 常开 **wire 默认就按全量落盘**——即便不开 `--trajectory`，`agents/<id>/wire.jsonl` 本就持久化完整的 prompts / messages / tool 结果。换言之 **SUMMARY 是相对默认 wire 的隐私收敛**（去掉了最大的 payload），而默认 wire 才是最大的常驻隐私面。要让重型 payload 不落盘，用 `--trajectory --trajectory-level summary`。
> - SUMMARY 的整形**刻意收窄**到 `llm_request.messages` 与 `tool_result.result` 这两个最大 payload。`tool_call.input`（write_file 的 content、run_shell 的命令）与 `assistant_message` 文本/`tool_uses` 在 SUMMARY 下**仍保留全量**（投影层依赖它们）。故 SUMMARY 并非完全脱敏——勿把密钥作为工具参数传入；完全输入脱敏留作后续工作。

### 读侧：`nanocode trajectory`

只读子命令，把常开 wire 投影为 trajectory（绝不写回 wire、绝不驱动 runtime）：

- `nanocode trajectory`（= `list`）—— 列出可投影为 trajectory 的 wire 会话。
- `nanocode trajectory show <id>` —— 逐 step 投影表 + 指标摘要（`trajectory.project_session` + `compute_metrics`）。id 支持前缀匹配与 `latest`。
- `nanocode trajectory export <id> [--out DIR]` —— 导出 trajectory bundle，打印 bundle 路径。

导出的 bundle（默认落 `session_root/trajectory/`，与 wire 同 session 根但**独立子目录**）含 4 个文件：

| 文件 | 内容 |
| --- | --- |
| `metadata.json` | trajectory-level 元数据（trajectory_id / model / 起止时间 / final_status / token 总数 / n_steps） |
| `steps.jsonl` | 每行一个 step 投影（observation → action → result → next_state → reward / done） |
| `metrics.json` | harness 聚合指标（turns / tool_calls / tokens / 估算费用 / 测试通过失败 / 高风险动作 …） |
| `evals.jsonl` | 在线启发式 eval / reward 信号 |

> 注意：`metrics` / `evals` 是**派生标签**，只落上述文件，**绝不**触碰 wire。空 / 缺失 session 也产出合法的 4 文件 bundle（空内容），导出绝不崩。

实现：`src/nanocode/trajectory/`（`project.py` / `metrics.py` / `eval.py` / `export.py` / `schema.py`，纯读侧派生）+ `src/nanocode/entrypoints/trajectory_cmd.py`（命令处理器）。
