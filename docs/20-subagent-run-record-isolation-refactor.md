# Subagent Run Record 与隔离改造方案

> 状态：最终改造方案，2026-06-21。
> 范围：background subagent、AgentRunRuntime/RunLedger、resume/steer、parallel worktree isolation、fresh/fork context、compaction-safe persistence。
> 约束：不做老旧兼容和兜底，不引入 OpenCode 的 SQLite/HTTP server 权威层，不兼容 Pi package 运行时，只吸收源码层设计；run record 不能成为第二套 session truth。

## 1. 结论

nanocode 的 subagent 改造采用一个混合方案：

1. **身份模型参考 OpenCode**：`run_id == child_session_id`。subagent run 的一等身份就是 child session，不再另造 `agent-001` 这类权威 id；`task-001` 只保留给 shell / memory 等 background host job。
2. **持久状态参考 Pi extension**：child session 下放 durable operational run record，形态类似 `status.json`、`events.jsonl`、`prompt.md`、`result.md`。父 agent 通过结构化 `get_subagent_result` 读取，不解析 `task_output` 文案。
3. **状态服务形态参考 Pi extension**：借 Pi extension 里最好的 service/state/observer/queue 形状，提供可序列化 snapshot、observer 累计指标、FIFO background limiter、rebind replay。
4. **嵌入式边界保持 nanocode/Pi 风格**：父/子 session 的 `session.jsonl` 仍是 canonical entry tree；run record 是 child-session-owned operational sidecar；worktree、steer、result 查询都由 host/runtime 管，不暴露成混杂的模型上下文。

OpenCode 的好处是 child session 身份统一，父子链接清楚；短板是 background registry 是 process-local，不抗进程重启。Pi extension 的好处是把长期运行状态做成 service/state/observer/queue 并能 replay，也常用 append-only JSONL 和 markdown prompt 文档抗 compaction/restart。nanocode 应取二者组合，而不是复制任一方。

### 1.1 Authority Boundary

硬不变量：

- `session.jsonl` 是 session identity、parent/child lineage、conversation transcript、replay、compaction 的唯一权威来源。
- `subagent-run/` 只能是 child-session-owned operational sidecar，用于 run lifecycle/status/metrics/result pointer 和 pending steer；它不能创建 session、改写 parent/child 血缘、替代 transcript replay，或作为 `/resume` / `/tree` 的权威索引。
- `RunLedger` 的发现顺序必须从 session 权威出发：先扫描 child `session_start.data.parentSession` 找到当前 parent 的 child sessions，再读取对应 child session 下的 `subagent-run/`。禁止反过来扫描 sidecar 并发明 child session。
- `prompt.md`、`result.md`、`status.json` 可以保存人类可读摘要和 entry pointer；若 sidecar 与 child `session.jsonl` 冲突，审计与 replay 以 child `session.jsonl` 为准。
- `pending_steer.jsonl` 是操作队列，不是 conversation history。steer 被应用后，实际 user turn 必须通过 child `AgentSession.record_event()` 写入 child `session.jsonl`。

## 2. 参考实现取舍

### 2.1 OpenCode

OpenCode 的 `task` tool 做了三件值得吸收的事：

- fresh task 创建 child session，并把 `parentSessionId`、`sessionId`、`model`、`background` 放进 tool metadata。
- `task_id` 指向 child session；resume 时按 `task_id` 打开同一个 session。
- background job id 也使用 child session id，因此工具返回的 `<task id="...">` 和 child session 是同一个身份。

但 OpenCode 的 `BackgroundJob` registry 是进程内状态。它适合 live 调度，不适合作为 nanocode 的 durable truth。因此 nanocode 只吸收 OpenCode 的 **identity 和 child-session link**，不吸收它的 process-local registry 作为权威状态。

### 2.2 Pi extension

Pi core 不把 subagent/run record 做成内建模型；长期状态通常由 extension 自己落在文件系统 sidecar，再用事件 hook、command、custom tool 和 steer/follow-up 消息驱动 agent 继续跑。Pi extension 能做这些，是因为官方 extension API 给了足够的 host hook：

- custom tool / command：extension 可注册模型可调用工具和 slash command，提供 `status/resume/output/cancel` 这类 action surface。
- lifecycle events：`session_start`、`session_shutdown`、`session_before_compact`、`session_compact`、`agent_end` 让 extension 能在启动、切会话、compaction 和 turn 结束后 replay 或继续任务。
- tool events：`tool_call`、`tool_result` 可作为 observer 输入，累加工具调用、测试、编辑、提交等进度。
- steering：Pi core 区分 `deliverAs` 和 `triggerTurn`。`deliverAs: "steer"` 决定 streaming 时排队到当前 turn/tool 后、下一次 LLM call 前；`deliverAs: "followUp"` 等 agent 本来要停时继续；`deliverAs: "nextTurn"` 只排到下一次用户 prompt 前，不唤醒、不打断；`triggerTurn: true` 才表示 idle 时立即启动一轮。
- session persistence / custom entry：`pi.appendEntry()` 允许 extension 写持久 entry；但高频运行态通常仍由 extension-owned files/JSONL 承担，避免污染主对话上下文。

Pi extension 生态里有几类模式可借鉴，分成源码已核对和生态参考：

- 已核对源码的模式：
  - `nicobailon/pi-subagents`：background run 使用 `status.json`、`events.jsonl`、`subagent-log-*.md`、result JSON 做可查询运行记录；`status/resume/interrupt` 读这些文件。live resume 先 interrupt live child，再经 intercom 投递 follow-up；terminal resume 从 stored child session file revive。这个证明 run record 值得借，但 nanocode 不应继承 intercom-reachable 才能 live resume 的限制。
  - `nicobailon/pi-messenger`：registry、inbox、feed、file reservation 是独立 mesh layer，incoming message 用 `pi.sendMessage(..., { triggerTurn: true, deliverAs: "steer" })` 唤醒 agent，不塞进 subagent tool。
  - `pi-ralph-wiggum`：循环状态落磁盘，在 `session_start` 从 state file 重新 hydrate，修复 auto-compaction 或 `/compact` 后内存 loop 丢失的问题。
  - `files-widget` / Pi examples：idle 时直接触发 user message，busy 时用 `followUp`；外部 file-trigger 显式 `triggerTurn: true`，plan-mode 用 `triggerTurn: false` 展示状态、`triggerTurn: true` 执行计划。
- 生态参考模式：
  - `pi-autoresearch-harness`：在隔离 worktree 下维护 `autoresearch.jsonl` 和 `autoresearch.md`。JSONL 是 append-only research history，markdown 是 fresh agent 可读的任务记忆。
  - `pi-multiloop`：在 `.multiloop/registry.json`、`state.json`、`results.jsonl`、`lessons.md` 中区分索引、resume snapshot、append-only history、长期策略笔记，并在 compaction 后发送 loop-aware resume prompt。
  - `pi-crew` 类扩展：适合借 team/workflow 的 durable run state 和 action surface，不适合替换 `agent`。它的 run state 目录和 locks 不应照搬进 nanocode；对 nanocode 只作为后续 TeamRuntime/workflow 参考，subagent run sidecar 仍必须挂在 child session 下。
  - `rpiv-todo` 类扩展：适合借 compaction 抗性的 replay 思想，在 `session_start/session_compact/session_tree` 时从 branch snapshot 重建状态；但 subagent run 不能把权威状态塞进 toolResult `details`。

nanocode 的对应选择：

- run record 放在 child session 目录下，而不是项目根 `.auto/`、全局 messenger 目录或 toolResult details。
- JSONL 作为事件源，`status.json` 作为可快速读取的原子快照，`prompt.md` 和 `result.md` 作为人类可读边界。
- child session `session.jsonl` 作为 session identity、parent lineage、transcript、replay/compaction 权威；child-owned run record 只是 lifecycle/status/metrics/result pointer 的 operational sidecar。
- messenger/mesh/file reservation 后续归 `TeamRuntime` 或 messenger runtime，不能塞进 parent-child runner。

## 3. 六个硬约束

### 3.1 Background subagent 必须有可查询 run record

每个 background subagent 创建 child session，并在 child session 下创建：

```text
~/.nanocode/sessions/<child_session_id>/
  session.jsonl
  subagent-run/
    status.json
    events.jsonl
    prompt.md
    result.md
    pending_steer.jsonl
```

`child_session_id` 是唯一权威身份：

```text
run_id == child_session_id
```

不再把 `TaskRecord`、`SubAgentRecord`、`task_output` 文本作为结果权威。它们最多是运行期投影或 UI 展示。

`status.json` 是快速查询快照：

```json
{
  "schemaVersion": 1,
  "runId": "sess_child",
  "childSessionId": "sess_child",
  "parentSessionId": "sess_parent",
  "spawnEntryId": "ent_spawn",
  "toolCallId": "toolu_...",
  "agentType": "coder",
  "status": "running",
  "background": true,
  "contextMode": "fresh",
  "isolation": "worktree",
  "worktreePath": "/abs/path/to/worktree",
  "model": {"provider": "anthropic", "modelId": "claude-..."},
  "createdAt": "2026-06-21T00:00:00Z",
  "startedAt": "2026-06-21T00:00:01Z",
  "endedAt": null,
  "promptEntryId": "ent_child_prompt",
  "resultEntryId": null,
  "resultPath": null,
  "error": null,
  "pendingSteerCount": 0
}
```

`events.jsonl` 是 append-only lifecycle log。最小事件类型：

```text
created
session_ready
started
steer_queued
steer_applied
tool_call
tool_result
progress
completed
failed
cancelled
worktree_created
worktree_finalized
```

`prompt.md` 保存初始任务、agent type、context mode、worktree policy、父分支摘要。它是 compaction/restart 后重建任务意图的最低可读 artifact；对应的真实 user message 仍必须写入 child `session.jsonl`，并在 `status.json.promptEntryId` 中记录。

`result.md` 保存最终结果的查询副本。父 agent 调 `get_subagent_result(child_session_id)` 时读取 `status.json + result.md`，必要时补 `events.jsonl` tail。最终 assistant/result 对应的真实 conversation entry 仍在 child `session.jsonl`，并在 `status.json.resultEntryId` 中记录。禁止解析 `task_output` 文案来判断是否完成或抽取结果。

### 3.2 增加 steer/resume 语义

`agent` tool 的语义分清三类：

```json
{
  "prompt": "...",
  "type": "coder",
  "run_in_background": true,
  "context": {"mode": "fresh"},
  "isolation": "worktree"
}
```

```json
{
  "resume": "sess_child",
  "prompt": "continue from the previous result...",
  "wake": true
}
```

```json
{
  "steer": "sess_child",
  "prompt": "narrow the search to runtime/spawn.py",
  "delivery": "steer",
  "wake": false
}
```

规则：

- `resume`：终态或 idle child session 的 continuation，在同一个 child session 追加新 user turn。`resume` 默认 `wake=true`，因为它表达的是显式继续。
- `steer`：running child session 的 steering prompt，不创建新 child。`steer` 默认 `wake=false`，表达 queue-only，不自动唤醒 idle child。
- `delivery="steer"`：对齐 Pi steer，running child 在当前 turn/tool 后、下一次 LLM call 前注入。
- `delivery="follow_up"`：对齐 Pi followUp，running child 在本来要停止时继续。
- `wake=true`：显式触发 idle child 开一轮；只有 `resume` 或显式 `run_send(..., wake=true)` 能这样做。
- child session 已 `session_ready` 且 live runner 存在时，`steer`/`follow_up` 投递给 child runner；投递成功后必须通过 child `AgentSession.record_event()` 写入 child `session.jsonl`。
- child session 尚未 ready 时，`steer`/`follow_up` 追加到 `pending_steer.jsonl`，同时写 `events.jsonl: steer_queued`。
- child runner 启动并写 `session_ready` 后，按顺序 drain `pending_steer.jsonl`，每条先写入 child `session.jsonl`，再写 `steer_applied`。
- child 已进入 terminal 状态时，`steer` 拒绝；用户应使用 `resume`。

`pending_steer.jsonl` 的最小记录：

```json
{
  "id": "steer_...",
  "delivery": "steer",
  "wakeRequested": false,
  "prompt": "...",
  "queuedAt": "2026-06-21T00:00:00Z",
  "state": "queued"
}
```

这对应 Pi core 的 `deliverAs` / `triggerTurn` 分离，而不是只学 Pi messenger 的 `deliverAs: "steer"`。实现必须留在 subagent runtime 内，不能引入 messenger registry/inbox。

### 3.3 Parallel 写代码任务默认 worktree isolation

为 `agent` tool 增加显式隔离字段：

```json
{
  "isolation": "shared" | "worktree"
}
```

默认策略：

- `tasks[]` parallel 模式中，只读 agent 使用 `shared`。
- `tasks[]` parallel 模式中，具备写工具的 `coder/general/custom` agent 默认使用 `worktree`。
- 单个 foreground/fresh agent 默认 `shared`，除非显式 `isolation="worktree"`。
- background 写型 agent 推荐显式 `worktree`；如果进入 parallel fan-out，则由默认策略强制使用 worktree。

worktree 由 host runtime 创建和记录，不进入模型上下文作为可编辑元状态：

```text
${NANOCODE_HOME}/worktrees/<project_hash>/<child_session_id>/
```

`status.json.worktreePath` 记录实际路径。child agent 的 `cwd` 指向 worktree。父 agent 只收到 bounded diff summary 和 result link，不自动把子 worktree 的修改合并回主 working tree。

新增模块建议：

```text
src/nanocode/subagents/worktree.py
```

职责：

- 根据 parent cwd 和 child session id 创建 git worktree。
- 记录 base ref/commit、branch name、worktree path。
- 产出 diff summary。
- finalize 时保留、删除或标记待合并，由显式命令控制。

### 3.4 Fresh/fork context 必须显式化

subagent 默认是 fresh context：

```json
{
  "context": {"mode": "fresh"}
}
```

含义：

- child 拿到自己的 system prompt、任务 prompt、必要的 repo/env volatile context。
- 不继承父完整 messages。
- 不把父 transcript 硬塞进 child。

需要父上下文时必须显式：

```json
{
  "context": {
    "mode": "fork_summary",
    "fromEntryId": "ent_...",
    "summary": "..."
  }
}
```

或：

```json
{
  "context": {
    "mode": "branch_projection",
    "fromEntryId": "ent_...",
    "include": ["open_files", "modified_files", "last_user_goal"]
  }
}
```

禁止实现 `context.mode="fork_full"` 作为默认路径。若未来确有需求，也必须是显式高风险模式，并由 host 做 token budget 和敏感信息过滤。

### 3.5 Mesh 协作层不能混进 subagent tool

以下能力不属于 `agent` tool：

- agent registry
- inbox/outbox
- shared feed
- file reservation
- peer status
- crew planner/reviewer
- autonomous swarm claim/completion

这些能力后续归：

```text
src/nanocode/runtime/teams.py
src/nanocode/runtime/messenger.py   # 未来新增
```

原因：

- subagent 是 parent-child delegation，父负责 spawn、steer、result collection。
- mesh 是 peer collaboration，核心是 registry/inbox/feed/reservation。
- 两者的生命周期、权限、可观测性不同，混在 `agent` tool 会让 parent-child runner 背上协作协议，破坏嵌入式边界。

### 3.6 持久状态必须抗 compaction/resume/restart

compaction 只能影响父/子 session 的 model context projection，不能影响 child-owned run operational record。session identity、parent/child lineage、conversation transcript 和 replay 仍只以 `session.jsonl` 为准。

必须满足：

- 父 session compaction 后，父仍可通过 child session id 调 `get_subagent_result`。
- child session compaction 后，run record 不丢 `prompt.md`、`events.jsonl`、`status.json`、`result.md`。
- nanocode 进程重启后，`AgentRunRuntime.rebind()` 可通过 `RunLedger` 从 child `subagent-run/status.json` 重建 run projection。
- background 运行期 live runner state 丢失时，状态不能被误报为 completed；应从 run record 和 child session lock/session status 推导 `lost` 或 `interrupted`。
- pending steer 写在 `pending_steer.jsonl`，不是只放内存队列。
- rebind 后如果 child non-terminal 但没有 live runner，不自动 drain `pending_steer.jsonl`；pending steer 保持可见，`run_send` 拒绝直接追加并提示显式 `resume`。
- `resume` 时 pending steer 的处理必须显式：默认按 queue order 合并进 revived child turn；如果用户传入新的 prompt，则新 prompt 排在既有 pending steer 后，避免静默吞掉早先 steer。

## 4. 父子 session 契约

父 session 只写 bounded link，不写 child transcript。

foreground subagent：

- 父 assistant message 包含 `agent` tool_call。
- 父 tool_result 的 `details` 只包含 bounded projection，便于当前 turn 闭合和 UI 展示；它不是 subagent run 权威。字段包含：

```json
{
  "childSessionId": "sess_child",
  "taskId": "sess_child",
  "status": "completed",
  "resultPath": ".../subagent-run/result.md",
  "summary": "...",
  "tokens": {"input": 0, "output": 0},
  "worktreePath": null
}
```

background subagent：

- 父 tool_result 立即返回 `status="running"`，关闭 tool round。
- child 完成后只向父追加 bounded completion notice，内容包含 child session id、status、summary、result path。
- 完整结果只在 child `subagent-run/result.md`。
- completion notice 默认只写父 session 的 bounded notice，不自动触发父 agent turn。
- 只有 spawn 时显式 `notify.wake_parent=true`，或父 runtime 明确处在等待该 child 的 workflow state，才允许 completion notice 触发父 agent 继续一轮。
- 无论是否 wake parent，父后续读取完整结果都必须走 `get_subagent_result(child_session_id)`，不能解析 completion notice 文案。

父结果注入必须 pin 到 spawn 分支，而不是完成时 live leaf。`spawnEntryId` 写入 `status.json`，也写入 child `session_start.data.parentSession.entryId`。

## 5. `get_subagent_result`

新增专用查询工具或 host method：

```text
get_subagent_result(child_session_id, include_events=false, tail_events=20)
```

返回结构：

```json
{
  "childSessionId": "sess_child",
  "status": "completed",
  "summary": "...",
  "result": "...",
  "resultPath": ".../result.md",
  "eventsTail": [],
  "worktreePath": "...",
  "error": null
}
```

要求：

- 只读 `subagent-run/status.json`、`result.md`、`events.jsonl`。
- 不解析 `task_output` 文案。
- 不读取 child transcript，除非用户显式打开 child session 或查询 raw session。
- 对 running 状态返回当前 status 和 events tail，不伪造 result。
- `result.md` 与 child `session.jsonl` 审计冲突时，以 child transcript 为准；`get_subagent_result` 应暴露 `resultEntryId` 供用户跳转，而不是私自重写 transcript。

`task_output` 保留为 UI/日志查看工具，不再是父 agent 获取 subagent 结果的权威接口。

## 6. AgentRunRuntime / RunLedger

新增 `runs` 包作为 subagent run 的 host-owned runtime 层。它不替代 child session，也不暴露 live agent 给 extension/tool；它只负责把 child-owned run record 折叠成可查询、可恢复、可调度的 runtime projection。

```text
src/nanocode/runs/
  models.py          # AgentRunRecord, RunStatus, RunMetrics, RunEvent
  ledger.py          # append/read/replay child-owned run record
  runtime.py         # spawn/status/cancel/send/rebind reconcile
  queue.py           # FIFO background limiter
```

职责：

- `AgentRunRecord`：serializable snapshot，字段覆盖 `run_id`、`child_session_id`、`parent_session_id`、`status`、`agent_type`、`model`、`context_mode`、`isolation`、`worktree_path`、`metrics`、`result_path`、`error`。
- `RunMetrics`：从 observer 累加 `tool_uses`、`usage`、`turn_count`、`compaction_count`、`active_tools`、`last_event_at`。
- `RunLedger`：读写 child `subagent-run/`，append `events.jsonl`，atomic replace `status.json`，从 `status.json + events.jsonl + result.md` replay 出 `AgentRunRecord`。
- `AgentRunRuntime`：唯一调度入口，封装 `spawn`、`status`、`list`、`cancel`、`send/steer`、`resume`、`rebind`。
- `RunObserver`：订阅 child session/tool/runtime events，把 tool use、usage、compaction、turn 计数写入 `events.jsonl`，并刷新 `status.json`。
- `RunQueue`：FIFO background limiter；queued/running/terminal 状态都经 `RunLedger` 持久化。

这个分层对应 Pi extension 的 `service/state/observer/limiter` 形状，但落在 nanocode 的 Python runtime 内；外部工具只拿 snapshot，不拿 live child agent/session 对象。

依赖边界：

- `runs/ledger.py` 只能依赖 paths、JSON/JSONL、typed models、session metadata；不能 import `agent.engine`、provider backend、TUI 或 tools。
- `RunLedger` 不是新的会话权威层。它只读写 child-owned `subagent-run/`，并以 child session id 作为输入；它不能自行分配 session id 或建立 parent/child 关系。
- `AgentRunRuntime.rebind(parent_session_id)` 必须先通过 session header 扫描找到 child sessions，再调用 `RunLedger.replay(child_session_id)`；禁止通过扫描所有 `subagent-run/` 目录来反向发明 child。
- `AgentRunRuntime` 可持有 live runner projection，但不能把 live child agent/session object 暴露给 tools、extensions 或模型。
- 如果后续加入 Codex-inspired `RunGraph`，它只能是导航投影，来源为 session tree + run records；不能有独立持久 truth，也不能替代 `session.manager.children()`。

## 7. 工具面重切

`agent` tool 只负责 parent-to-child delegation：

- fresh foreground：阻塞直到 child 完成，返回 bounded result。
- fresh background：返回 `run_id`，child 继续跑。
- resume：续同一个 child session。
- steer/send：给 running child 追加 steering prompt。

新增 run 查询/控制工具：

```text
run_list
run_status
run_output
run_cancel
run_send
```

语义：

- `run_list`：列当前父 session 的 child runs，默认隐藏 terminal older runs，可带 status/filter。
- `run_status`：读 `AgentRunRecord` snapshot，不读完整 transcript。
- `run_output`：结构化读取 result/progress，等价于 `get_subagent_result` 的用户/模型工具面。
- `run_cancel`：取消 live run；若无 live coroutine 但状态 non-terminal，则通过 rebind reconcile 追加 `lost` 或 `interrupted` 事件，不伪造成功 cancelled。
- `run_send(child_session_id, prompt, delivery="steer"|"follow_up", wake=false)`：默认 queue-only；`wake=true` 才允许触发 idle child turn。running child 按 `delivery` 注入；session 未 ready 时排队 pending steer；terminal child 拒绝。

`task_list`、`task_output`、`task_stop` 只保留给 shell/background host jobs，不再混 subagent。当前 `TaskRecord(kind="subagent")` 和 `SubAgentRecord(id="agent-001")` 是旧投影，应从新实现里移除权威地位。

## 8. Queue / rebind / lost

后台并发控制借 Pi extension 的 FIFO limiter 形状：

- background run 创建后先写 `status="queued"`。
- 进入执行槽后写 `status="running"`。
- foreground run bypass queue，但仍写 run record。
- queue 状态由 `AgentRunRuntime` 管，不由模型或工具文案推断。

rebind/resume 规则：

- 进程启动、`/resume`、session switch 后，`AgentRunRuntime.rebind(parent_session_id)` 先从 session headers 找 child sessions，再读取这些 child 的 run records。
- terminal run 原样保留。
- non-terminal 且当前进程没有 live coroutine 的 run 写成 `lost` 或 `interrupted`，不能误报 `completed`。
- `lost` 是新状态写入，不覆盖旧事件；用户可显式 `resume` 开新 turn 或查看已有 output。
- pending steer 在重绑后仍可见；若 child 不再 live，`run_send` 拒绝并提示 resume。
- rebind 不从 sidecar 创建 child session，不补写 parent/child lineage；缺失或损坏的 child `session.jsonl` 是 hard error，而不是用 `status.json.parentSessionId` 兜底恢复。

## 9. 代码落点

新增：

```text
src/nanocode/runs/models.py
src/nanocode/runs/ledger.py
src/nanocode/runs/runtime.py
src/nanocode/runs/queue.py
src/nanocode/subagents/run_record.py
src/nanocode/subagents/worktree.py
src/nanocode/subagents/steer.py
src/nanocode/tools/get_subagent_result.py
src/nanocode/tools/run_list.py
src/nanocode/tools/run_status.py
src/nanocode/tools/run_output.py
src/nanocode/tools/run_cancel.py
src/nanocode/tools/run_send.py
```

修改：

```text
src/nanocode/tools/agent.py
src/nanocode/runtime/spawn.py
src/nanocode/agent/subagent_manager.py
src/nanocode/tasks/models.py
src/nanocode/tasks/manager.py
src/nanocode/tools/tasks_tool.py
src/nanocode/session/manager.py
src/nanocode/session/listing.py
src/nanocode/entrypoints/commands/builtin.py
src/nanocode/runtime/facade.py
```

职责拆分：

| 文件 | 责任 |
| --- | --- |
| `runs/models.py` | `AgentRunRecord`、`RunStatus`、`RunMetrics`、`RunEvent` |
| `runs/ledger.py` | replay child-owned run record，提供 append/read/update 原语 |
| `runs/runtime.py` | spawn/status/list/cancel/send/resume/rebind 的唯一 host runtime |
| `runs/queue.py` | FIFO background limiter，queued/running 状态持久化 |
| `subagents/run_record.py` | create/update/read `status.json`; append `events.jsonl`; write `prompt.md`/`result.md`; atomic write |
| `subagents/steer.py` | pending steer queue、ready drain、terminal-state rejection |
| `subagents/worktree.py` | host-owned git worktree lifecycle、diff summary、cleanup marker |
| `runtime/spawn.py` | 薄适配到 `AgentRunRuntime`；保留 child agent 构造细节 |
| `tools/agent.py` | schema 增 `context`、`isolation`、`steer`; 默认 fresh |
| `tools/get_subagent_result.py` / `tools/run_*` | 结构化 run 查询/控制 |
| `tasks/*` | 仅保留 shell/background host jobs；subagent 不再走 `TaskRecord(kind="subagent")` |
| `session/listing.py` | 顶层 session 隐藏 child；child 由 `/agents` 或 result 查询进入 |

## 10. 分阶段落地

### P0 文档和 schema 冻结

- 本文档作为最终方案。
- `agent` tool schema 确认：`context`、`isolation`、`resume`、`steer`、`run_in_background`。
- 明确删除老旧兼容：不保留 `agents/<agent-id>`、`agent-001` 作为 subagent/run 权威，不做 legacy alias fallback；`task-001` 仅作为 shell / memory 等 background host job id。

### P1 RunRecord + RunLedger

- 新增 `runs/models.py`、`runs/ledger.py`。
- 新增 `subagents/run_record.py`。
- background spawn 创建 child session 后立即创建 run record。
- foreground 也写 run record，但可以不写 `pending_steer.jsonl`。
- `RunLedger.replay(child_session_id)` 能从 run record 重建 `AgentRunRecord`。
- `get_subagent_result` / `run_output` 只读 run record。

验收：

- background 完成后，清空 live runtime projection 仍能从 run record 查询 result。
- `task_output` 文案变化不影响 `get_subagent_result`。
- `TaskRecord(kind="subagent")` 不再是 subagent 查询路径。

### P2 AgentRunRuntime + Steer/Resume

- 新增 `runs/runtime.py`。
- `resume=<child_session_id>` 续终态或 idle child。
- `steer=<child_session_id>` 投递 running child。
- `run_send(..., wake=false)` 只排队，不触发 idle child turn。
- `run_send(..., wake=true)` 才能唤醒 idle child。
- `delivery="steer"` 在当前 turn/tool 后注入；`delivery="follow_up"` 在 child 本来要停止时继续。
- session 未 ready 时写 `pending_steer.jsonl`。
- child ready 后 drain pending steer。

验收：

- before-ready steer 不丢。
- queue-only steer 不唤醒 idle child。
- wake steer 显式触发 idle child turn。
- running steer 不中断当前 tool execution。
- follow-up 只在 child 将要停止时注入。
- running steer 不创建新 child。
- terminal child steer 被拒绝，提示使用 resume。

### P3 Run tools + task 切分

- 新增 `run_list/run_status/run_output/run_cancel/run_send`。
- `task_list/task_output/task_stop` 只保留 shell/background host jobs。
- `/agents` 和 TUI panel 从 `AgentRunRuntime.list()` 派生。

验收：

- 模型查询 subagent 结果走 `run_output` 或 `get_subagent_result`。
- shell 后台任务仍走 `task_output`。
- subagent 不再混入 `TaskManager.list_tasks(kind="subagent")`。

### P4 Worktree Isolation

- parallel mutating tasks 默认 worktree。
- worktree path 写入 `status.json`。
- result 包含 diff summary。
- 不自动 merge。

验收：

- 两个 parallel coder 修改同一文件时不写同一 working tree。
- 父 working tree 在 child 完成前后保持不被直接修改。

### P5 Fresh/Fork Context

- 默认 fresh。
- 显式 `fork_summary` 和 `branch_projection`。
- 禁止隐式复制父完整上下文。

验收：

- child request 不含父 raw transcript。
- fork summary 只包含 summary/projection。

### P6 Queue / rebind / lost

- 新增 `runs/queue.py`。
- background queued/running/terminal 状态经 run record 持久化。
- rebind 先从 session headers 找 child sessions，再读取对应 child run record；无 live coroutine 时标记 `lost` 或 `interrupted`。
- rebind 不从 sidecar 反向创建 session，不修补 parent/child lineage。

验收：

- 超过并发限制的 background run 先进入 queued。
- 进程重启后 non-terminal run 不误报 completed。
- lost run 可被用户显式 resume 或查看已有 output。
- lost run 的 pending steer 仍可见，但不会自动 drain。

### P7 Compaction/Resume 抗性

- 从 child session header + run record 重建 `/agents` 列表。
- 父/子 compaction 后 `get_subagent_result` 仍可用。
- 进程重启后可从 `status.json` 重建 run projection。
- sidecar 与 child transcript 冲突时，审计/replay 以 child `session.jsonl` 为准。

验收：

- 父 session compact 后能查 running/completed child。
- child session compact 后 `prompt.md/events.jsonl/result.md` 不变。
- 重启后 lost/interrupted 状态可解释，不误报 completed。
- `resultEntryId` 可跳转到 child transcript；`result.md` 不能替代 transcript。

### P8 TeamRuntime 留白

- 不在 `agent` tool 增 registry/inbox/feed/reservation。
- 后续单独设计 `runtime/messenger.py` 或扩展 `runtime/teams.py`。

验收：

- subagent runner 不 import messenger/team registry。
- file reservation 不在 parent-child run record 内实现。

## 11. 测试清单

新增测试建议：

```text
tests/runs/test_run_ledger.py
tests/runs/test_agent_run_runtime.py
tests/runs/test_run_queue.py
tests/subagents/test_run_record.py
tests/subagents/test_steer_resume.py
tests/subagents/test_worktree_isolation.py
tests/tools/test_get_subagent_result.py
tests/tools/test_run_tools.py
tests/session/test_child_session_run_record.py
```

覆盖点：

- `status.json` atomic update。
- `events.jsonl` append-only 顺序。
- `prompt.md` 和 `result.md` 路径稳定。
- background running/completed/failed/cancelled 状态查询。
- pending steer ready 后 drain。
- `run_send(wake=false)` 不触发 child turn。
- `run_send(wake=true)` 在 idle child 上触发 turn。
- running steer 在当前 tool/turn 后注入。
- follow-up 在 child 将要停止时注入。
- terminal steer 拒绝。
- resume 复用同一 child session。
- `pending_steer.jsonl` 在 compaction/restart/rebind 后仍存在。
- lost child 不自动 drain pending steer，必须显式 resume。
- background completion notice 默认不触发父 turn，显式 wake 时才触发。
- parent compaction 后查询 child result。
- child compaction 后 run record 不变。
- parallel mutating tasks worktree 隔离。
- `/sessions` 隐藏 child，`/agents` 显示 child。
- `run_*` 和 `task_*` 查询面不混。
- rebind 后 non-terminal orphan run 标记 lost/interrupted。
- `RunLedger.rebind` 先扫 session headers，再读 sidecar；不会从孤立 sidecar 发明 child session。

## 12. 明确不做

- 不兼容旧 `agents/<agent-id>` 目录作为权威。
- 不继续把 subagent 塞进 `TaskRecord(kind="subagent")`。
- 不为旧 `task_id` 文案解析做 fallback。
- 不把父完整上下文默认 fork 给 child。
- 不把 Pi messenger 的 registry/inbox/feed/file reservation 混进 `agent` tool。
- 不引入 OpenCode SQLite/HTTP/API 作为 nanocode 权威层。
- 不把 pi-crew 的 `.crew/state/runs/...` 目录结构搬进 nanocode。
- 不把 toolResult `details` 当 subagent run 权威。
- 不把 `subagent-run/status.json` 当 session identity、parent/child lineage、transcript replay 或 compaction 的权威。
- 不从孤立 `subagent-run/` 目录反向创建 child session。
- 不自动 merge child worktree。
- 不做任意深度 recursive subagent。

## 13. 与现有文档关系

- 本文档细化并修正 `docs/13-pi-session-tree-migration.md` 的 subagent 部分。
- 若本文档与旧文档中 `legacyAgentId`、`agents/<agent-id>` 迁移兼容、`task_output` 结果查询等描述冲突，以本文档为准。
- `docs/13` 仍保留 session tree、child session header、父子索引、单写者 lock 的总体设计。

## 14. 参考链接与源码锚点

这些链接用于设计取舍和源码核对，不表示 nanocode 要兼容对应 package 的运行时格式。

### OpenCode

- Task tool child-session identity：`task_id` 指向 child session，metadata 含 parent/child session/model/background，background job id 也复用 child session id。
  - https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/task.ts
- BackgroundJob registry：process-local background registry，适合 live 调度，但不是 durable truth。
  - https://github.com/anomalyco/opencode/blob/dev/packages/core/src/background-job.ts
- Session model：session row 含 `parent_id`，children/list roots 基于 parent 关系。
  - https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.ts

### Pi core / docs

- Pi extension API：custom tool/command、lifecycle events、tool events、`pi.sendMessage`、`pi.appendEntry`、custom compaction 等能力来源。
  - https://pi.dev/docs/latest/extensions
- Pi core `sendCustomMessage` / `sendUserMessage`：`deliverAs` 与 `triggerTurn` 分离，`nextTurn` queue-only。
  - https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/agent-session.ts
- Pi core agent loop：steer 在当前 turn/tool 后、下一次 LLM call 前 drain；follow-up 在 agent 本来要停止时 drain。
  - https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent-loop.ts
  - https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent.ts
- Pi durable harness 设计笔记：方向上要求 queued steer/followUp/nextTurn 可持久化，但当前 harness 队列仍是 runtime state，不能照搬成 nanocode session truth。
  - https://github.com/earendil-works/pi/blob/main/packages/agent/docs/durable-harness.md
- Pi packages manifest/distribution model。
  - https://pi.dev/docs/latest/packages
- Pi package catalog：用于发现 extension 生态，不以下载量判断架构质量。
  - https://pi.dev/packages
- Pi extension examples：`file-trigger` 使用 `triggerTurn: true` 唤醒，`plan-mode` 用 `triggerTurn: false` 展示状态、`triggerTurn: true` 执行计划。
  - https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/README.md
  - https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/file-trigger.ts
  - https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/plan-mode/index.ts

### Pi extension packages

- `nicobailon/pi-subagents`：background run record、status/resume/interrupt、live intercom resume、terminal revive、worktree isolation 的源码参考。
  - https://github.com/nicobailon/pi-subagents
  - https://github.com/nicobailon/pi-subagents/blob/main/src/runs/background/subagent-runner.ts
  - https://github.com/nicobailon/pi-subagents/blob/main/src/runs/background/async-resume.ts
  - https://github.com/nicobailon/pi-subagents/blob/main/src/runs/foreground/subagent-executor.ts
  - https://github.com/nicobailon/pi-subagents/blob/main/src/runs/shared/worktree.ts
- `pi-autoresearch-harness`：`autoresearch.jsonl` append-only history + `autoresearch.md` living document + isolated worktree，是 `events.jsonl/prompt.md/result.md/status.json` 形态的主要参考。
  - https://pi.dev/packages/pi-autoresearch-harness
- `pi-multiloop`：`.multiloop/registry.json`、`state.json`、`results.jsonl`、`lessons.md`，以及 explicit resume / compaction-aware continuation。
  - https://pi.dev/packages/pi-multiloop
- `pi-messenger`：registry/inbox/feed/file reservation 是独立 mesh layer，使用 lifecycle/tool hooks 和 steer 唤醒，不应塞进 `agent` tool。
  - https://github.com/nicobailon/pi-messenger
  - https://github.com/nicobailon/pi-messenger/blob/main/index.ts
  - https://github.com/nicobailon/pi-messenger/blob/main/feed.ts
- `tmustier/pi-extensions`：`pi-ralph-wiggum` 的 disk state rehydrate 和 `followUp` loop、`files-widget` 的 idle vs busy delivery、`session-recap` 的 lifecycle handling。
  - https://github.com/tmustier/pi-extensions
  - https://github.com/tmustier/pi-extensions/blob/main/pi-ralph-wiggum/index.ts
  - https://github.com/tmustier/pi-extensions/blob/main/files-widget/index.ts
  - https://github.com/tmustier/pi-extensions/blob/main/session-recap/index.ts
- `@gotgenes/pi-subagents`：in-process subagent core，参考 service/state/observer/limiter 形状。
  - https://pi.dev/packages/%40gotgenes/pi-subagents
  - https://github.com/gotgenes/pi-packages
- `@gotgenes/pi-subagents-worktrees`：subagent worktree isolation 的 package 形态参考。
  - https://www.jsdelivr.com/package/npm/%40gotgenes/pi-subagents-worktrees
- `pi-crew`：durable state、async/background runs、parallel execution、worktree isolation，适合 TeamRuntime/workflow 参考，不替换 parent-child `agent` runner。
  - https://pi.dev/packages/pi-crew
  - https://github.com/baphuongna/pi-crew
- `rpiv-todo`：conversation/session lifecycle replay 和 compaction survivability 的参考；不把 toolResult details 当 subagent run 权威。
  - https://github.com/juicesharp/rpiv-todo
  - https://github.com/juicesharp/rpiv-mono
  - https://www.npmjs.com/package/%40juicesharp/rpiv-todo
- 其他 subagent/worktree/team 类生态参考，仅用于横向观察：
  - https://github.com/tintinweb/pi-subagents
  - https://github.com/hazat/pi-interactive-subagents
  - https://github.com/pasky/pi-side-agents
  - https://github.com/tintinweb/pi-tasks

### nanocode 本仓库源码落点

- Current session tree and child-session header/navigation precedent：
  - `src/nanocode/session/tree.py`
  - `src/nanocode/session/manager.py`
- Current subagent spawn path and background execution：
  - `src/nanocode/runtime/spawn.py`
  - `src/nanocode/agent/subagent_manager.py`
  - `src/nanocode/agent/engine.py`
- Current task/subagent projection that should be split：
  - `src/nanocode/tasks/models.py`
  - `src/nanocode/tasks/manager.py`
  - `src/nanocode/tools/tasks_tool.py`
- Agent tool schema and future run tools：
  - `src/nanocode/tools/agent.py`
  - future `src/nanocode/tools/run_list.py`
  - future `src/nanocode/tools/run_status.py`
  - future `src/nanocode/tools/run_output.py`
  - future `src/nanocode/tools/run_cancel.py`
  - future `src/nanocode/tools/run_send.py`
- Runtime/facade and command wiring：
  - `src/nanocode/runtime/facade.py`
  - `src/nanocode/entrypoints/commands/builtin.py`
- Future run runtime package：
  - future `src/nanocode/runs/models.py`
  - future `src/nanocode/runs/ledger.py`
  - future `src/nanocode/runs/runtime.py`
  - future `src/nanocode/runs/queue.py`
