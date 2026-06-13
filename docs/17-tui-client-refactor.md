# docs/17 — TUI 客户端化重构（Pi 对齐，激进无兼容）

## 背景与目标

重构前，nanocode 的 agent core 与 TUI 之间存在**两套并行输出机制**，而 TUI 用的是过时的那套：

- **旧路（push-into-core）**：`agent/core.py` 循环里直接调 `cfg.sink.spinner_start/stop/cost/info/retry`；
  `Agent.emit()` 扇出 `runtime_events.project_agent_event(event, self._sink)`，由 `EventSink`
  （`agent/sink.py`：TerminalSink/NullSink/BufferSink/TeeSink）渲染。**core 在循环里命令式驱动渲染**，
  且一半的“事件”（spinner/info/cost/retry/sub_agent/confirmation）**只以 sink 方法存在，从未成为
  AgentEvent**。
- **新路（pull stream，已建好但无人渲染）**：`Agent.emit()` 同时把 typed `AgentEvent` 推给
  `_event_subscribers`；`RuntimeThread` tap 这条腿，包成 `{thread_id,session_id,seq,type,event}` 信封，
  留 512 环形缓冲（`events()`）并暴露 `subscribe(listener)->unsubscribe`。

**Pi 参照**（`/private/tmp/pi-src`）只有**一套**机制：core 只 `emit(AgentEvent)`、永不渲染；TUI 是
`session.subscribe(handleEvent)` 的纯订阅者（`modes/interactive`）；RPC mode（`modes/rpc`）用
JSON-over-stdio 驱动同一 session 证明解耦；`packages/tui` 零 agent 依赖。

**目标**：把 core 仅剩的命令式渲染调用反转成事件、渲染整体推到订阅端的 `TerminalClient`、审批改
请求/响应、并以 RPC/headless mode 钉死边界。**激进、无旧兼容**：同一改动里删除 sink 机构。
**不重建** runtime/session 栈（docs/14 已 Pi 对齐）。

不可回归不变量：allowlist fail-closed、SessionLease 写者租约、abort/cancel 优雅取消、
`final_response`/sub-agent 结果捕获、`record_event` 树先于 UI、子 agent 事件结构隔离。

## 终态架构

```
core (engine/core/session/spawn)  ── 唯一出口 Agent.emit(AgentEvent) ──▶ [record_event(树), _event_subscribers]
                                                                                              │
                                                            RuntimeThread tap → 信封流 → subscribe(listener)
                                                                                              │
                                              ┌───────────────────────────────────────────────┼─────────────┐
                                       TerminalClient(订阅渲染)                          RPC mode(stdout JSON)   未来其它 client
```

core 不再认识 rich/终端/spinner/EventSink。渲染、spinner 派生、审批显示全在订阅端。

## 事件模型（`agent/events.py`）

UI-only 事件（`DURABLE_ENTRY_FOR_EVENT=None`，无树等价物）：
- `NoticeRaised(text, level)` — 自由文本诊断（取代散落 `self._sink.info`）。**纪律**：已有 typed
  事件的（BudgetExceeded / ToolCallAuthorized deny / session_switch 边界）渲染那些事件，绝不退化为
  info sink 别名。
- `RetryRaised(attempt, max_retries, reason)` — provider 流重试（旧 `sink.retry`）。
- `SubAgentStarted/Ended(agent_type, description)` — 子 agent/skill-fork 起止（旧 `sink.sub_agent_*`）。
- `ApprovalRequested(command, message, request_id)` — 危险动作审批**显示**事件（旧 `sink.confirmation`）。

`TurnCompleted` additive 加 `cost_usd`（emit 时算好，订阅端含 RPC 直接显示成本）。

spinner / cost **client 派生**（不再是事件）：`LlmRequestPrepared` 起 spinner，首个内容/终态事件停；
cost 从 `TurnCompleted` 渲染。唯一可接受时序偏移：spinner 在首个 AssistantDelta（block 粒度）停，
略晚于旧 StreamCallbacks 首-token stop——纯视觉、无语义影响。

## 实施阶段（均已落地，每阶段独立提交 + 全量绿）

- **Phase 0**（`0f7ae00`）：`final_response`/子 agent 文本捕获从 `BufferSink` 改为 `Agent._final_text_chunks`
  累加器（emit 见 `AssistantDelta.text` 即 append，每轮入口 reset）——先拆最危险的雷。
- **Phase 1**（`0520a84`）：新增 `entrypoints/terminal_client.py`；assistant/tool 三类流式渲染从
  `project_agent_event` 迁到 `TerminalClient.on_event`（订阅）；删 `runtime_events.py`；`RuntimeHost`
  持 client、thread 替换时重订阅。
- **Phase 2**（`c706b63`）：sink-only 表现（spinner/cost/info/retry/sub_agent/budget/deny）全部升格为
  事件或 client 派生；core 的 `cfg.sink.*` 清零（除 confirmation）。
- **Phase 3+4**（`489b81a`）：审批改请求/响应（`ApprovalRequested` 事件 + 注入 `confirm_fn`；无回调
  fail-closed deny，杀掉阻塞 `input()` 泄漏）；删 `agent/sink.py` 全套 + `sink` 参数贯穿面 +
  `capture_response`/TeeSink + `CommandContext.out`。
- **Phase 5b+5c**（`3ed9328`）：`entrypoints/rpc.py` + `--rpc`（验收试金石）；`mcp/manager.py` print
  泄漏改 `notify` 回调 → `NoticeRaised`。
- **Phase 5a**：`RuntimeThread.status()` 收口 footer 对 Agent 私有面的读取。

## RPC / headless 协议（`entrypoints/rpc.py`）

行分隔 JSON。stdin：`{"cmd":"prompt","text"}` / `{"cmd":"cancel"}` /
`{"cmd":"approval_response","approved":bool}` / `{"cmd":"exit"}`。stdout：每条 AgentEvent 信封逐行
JSON + `{"type":"turn_result",...}`。审批往返：core 的 async `confirm_fn` 挂起 turn，stdin 的
`approval_response` 解决 pending future（FIFO，turn 内审批串行）；`ApprovalRequested` 携 `request_id`
出 stdout 供外部回显。turn 作为独立 task 跑，stdin 持续可读（cancel/approval 不被阻塞）。

## 验证

- 全量 pytest（基线 ~1430）绿。
- `tests/agent/test_terminal_client.py`：事件→渲染映射、spinner 派生（起/停/retry 不停）、cost、approval 显示。
- `tests/entrypoints/test_rpc_mode.py`：stdio 驱动 turn + 事件流、审批批准往返、审批拒绝挡工具。
- `--rpc` 真子进程冒烟通过。

## 遗留（follow-up，非本次范围）

- **命令层从 Agent 私有面断奶**（B-list）：`entrypoints/commands/builtin.py` 仍 reach
  `agent._session_mgr` / `_spawn_*` / `_background_tasks` / `task_manager` / `agent_session.clear_history`。
  这是「稳定命令 API」的正交清理（与「TUI 是 client」解耦目标不强绑定）；footer 已先经
  `RuntimeThread.status()` 收口。把这些提升为 `RuntimeThread`/`AgentRuntime` 方法后，RPC 客户端方可
  驱动 slash 命令。
- MCP 连接成功日志原 verbose-gated，现随事件流出（失败仍始终可见）——可在 client 侧按 verbose 过滤
  info-level 通知。
