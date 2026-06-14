# 12 · 可嵌入式 Agent 分层改造（修订版）

> 日期：2026-06-09（修订版）
>
> 原稿见 `12-embeddable-agent-layered-refactor.orig.md`。本修订版基于对当前代码树的逐条核实与多视角批判，修正了原稿的若干事实/排序/架构问题，并与 `docs/09`（runtime-platform）、`docs/11`（command-layer）对齐。
>
> 目标不变：把 nanocode 从"单 CLI coding agent"演进为"可嵌入式 agent runtime"——**内部靠近 Pi**（事件化 session、per-agent event tree、branch/fork、context builder、append-only 审计），**外部靠近 Codex**（thread/turn/approval/runtime facade、App Server、JSON-RPC、SDK、多客户端）。

### 相对原稿的实质修订

```text
1. [Blocker] 推荐 PR 顺序原把 CMD-P2.5（走 runtime）排在 registry 抽取之前 —— 反了依赖，已重排为 CMD-P0→P1→P2→P2.5。
2. 层模型与 docs/09 对齐：补回被删的 L3 "Event-Sourced Session Runtime" 层；新增跨文档编号 crosswalk 表。
3. App Server / Protocol 从"每个 client 必经的层"改写为"L4 Runtime 的传输适配器"（一套 Runtime API，两种绑定）。
4. 事实纠正：PermissionEngine 非"绝对唯一咽喉"（!shell 例外）；resume 已是 rebuild-first；ApprovalManager/RuntimeThread 对 live CLI 仍是 dead code。
5. 补回原稿缺失的 load-bearing 工作：bootstrap-owning create_session(config)、RuntimeThread.events() 流、审批 park/respond、写侧事件统一（先于 EventBus）、headless -p 路径、每阶段测试/回滚。
6. Phase A 不再重述 doc11，改为引用并沿用其不变量与 characterization merge gate。
```

---

## 结论

当前 nanocode **方向符合可嵌入式 agent 的要求**，且底座比原稿描述的更成熟（事件 spine 与 runtime/session 类已落地，见 docs/09:1327-1334 的 event-spine 进度行、docs/11:78-85；docs/09 同区旧的“resume 仍以 snapshot 为权威” bullet 已被当前代码更新为 rebuild-first，见 Layer 3）。仍处"单包内分层过渡态"：

- 已有关键 seam：`AgentRuntime`、`RuntimeThread`、`TurnResult`、`ApprovalManager`、`AgentSession`、`MessageStore`、`CompressionPipeline`、`EventSink`、`PermissionEngine`、per-agent `wire.jsonl` 事件树。
- 还没有：`EventBus`、`AppServer`、JSON-RPC、SDK、`SubAgentManager`、`SandboxManager`、`CapabilityRouter`，以及**拥有 bootstrap 的 runtime 入口**。
- 真正剩余的活比"建 runtime"窄得多：**把 `run_repl` 接到既有 runtime（不再 reach 进 `Agent` 私有面）+ 抽 capability/persistence facade**。

推荐定调：

```text
先稳定边界，再移动目录；先让 CLI 自己成为 in-process runtime client，再做 JSON-RPC/App Server/SDK。
命令层直接复用 docs/11 的 CMD-P0..P2.5，不另起一套。
```

---

## 目标分层（以 docs/09 为准）

采用 docs/09:131-157 的规范分层（原稿删掉了 L3 Session Runtime 并把 Persistence 提为 L1，导致"Layer 2/3"在两文里指不同概念——本版改回对齐）：

```text
Layer 7  Clients
         CLI / Python SDK / IDE / Web UI / CI scripts

Layer 6  Protocol & Transports        ← App Server 的序列化边缘，非每个 client 必经
         JSON-RPC methods, stdio JSONL, unix socket, websocket

Layer 5  App Server                    ← out-of-process 传输适配器，非独立运行时（见"关键边界 1"）
         long-lived process, thread registry, request router, event fanout, approval parking

Layer 4  Runtime Facade
         AgentRuntime, RuntimeThread, turn lifecycle, TurnResult, ApprovalManager, events()

Layer 3  Event-Sourced Session Runtime
         AgentSession, SessionContextBuilder, per-agent wire 事件树,
         SessionStore / EventLog / SnapshotStore / ArtifactStore（持久化收口于此层）

Layer 2  Agent Core
         model loop, MessageStore, CompressionPipeline, tool-call scheduling

Layer 1  Capabilities
         tools, MCP, skills, hooks, permissions, sandbox, subagents（经 CapabilityRouter，统一过 PermissionEngine）

Layer 0  Platform Adapters
         Anthropic/OpenAI backend, filesystem, shell sandbox backend, terminal UI sink
```

**依赖方向单向**（docs/09:159）：上层调用下层，下层不能 import 上层。这是首要落地目标——先作为依赖方向落地，而非马上拆发行包。

> 注：原稿的"Persistence"层在此并入 L3 Session Runtime 的 stores（持久化 facade 仍是 Phase C 的真实交付物，只是不单列为一个 tier）。

---

## 阶段 / 编号对照表（crosswalk）

仓库已有四套互撞的 `P`/阶段编号（docs/11、docs/09、roadmap、devlog subagent），本文是第五套；故给出唯一对照表。**裸 `P5` 等 token 在不同文档指不同工作，不可跨文比较。**

| 本文 Phase | 本文 PR 标签 | == docs/11 | ≈ docs/09 | ≈ roadmap doc |
|---|---|---|---|---|
| A · CLI client 化 | **CMD-P0→P1→P2→P2.5**（见 doc11，不重述） | CMD-P0..P2.5 | P-1(部分)/P2(EventSink) | P3(wire) |
| B · Capabilities 边界 | CAP-P1(SubAgent)/CAP-P2(Sandbox)/CAP-P3(Router) | — | P0.5(PermissionEngine 咽喉) | P4(agents 面) |
| (新) Runtime 拥有 bootstrap | **RUNTIME-P0**(create_session/thread_start) | — | runtime/bootstrap 目标 + docs/11:249（≠P-1 正文） | — |
| C · Persistence facade | PERSIST-P1 | — | (L3 stores) | — |
| (新) 审批 park/respond | **RUNTIME-P2** | — | P6(approval 协议) | — |
| D · EventBus + App Server | EVENT-P1(RuntimeEvent 流+sinks)/EVENT-P2(EventBus)/SERVER-P1 | — | P6/P7 | — |
| E · Protocol & SDK | PROTO-P1/SDK-P1 | — | P7/P8 | — |
| F · Pi-style P5 完成 | PI-P5 | — | **P5** | P5(JIT，含义不同) |

> devlog 的 subagent `P1-P4`（`feat/subagent-p1-p4`）是上述四套既有轴之一，与本表的 Phase/PR 标签无映射；提及子 agent 工具限制时用全名，勿用裸 `P4`。

---

## 当前实现状态

### 已经存在的关键 seam（已逐条核实）

| 能力 | 当前实现 | 评价 |
|---|---|---|
| Runtime facade | `runtime/facade.py` | `AgentRuntime`/`RuntimeThread`/`TurnResult`/`ApprovalManager` 已迁入 runtime 层；`agent/runtime.py` 仅作兼容 re-export。 |
| Agent session | `session/agent.py` | `AgentSession` 是 state↔canonical tree 同步与 turn shell；`agent/session.py` 仅作兼容 re-export。 |
| Message owner | `agent/message_store.py` | `MessageStore` 已收口；getter 返回 live list（in-place 操作不变），整表赋值经 setter→`store.load`（engine.py:501-515）。 |
| Context builder | `agent/context_builder.py` | snapshot/event-tree resume/fork 入口已有；忠实才采用 rebuild、否则回退 snapshot。 |
| Compression | `agent/compaction.py` | `CompressionPipeline` 收口 **budget/snip/microcompact**：owner 在 `engine.py:669-682 _run_compression_pipeline`（调 `prepare_openai/anthropic`），由模型循环每轮在 `backends:70` 调用（每次 provider call 前 in-place 跑）。注：**LLM 摘要式 full compaction 不在 pipeline**——由 `backends:53 _check_and_compact` 在 turn 边界触发 `engine.py:610 _compact_conversation`。 |
| UI sink | `agent/sink.py` | `EventSink`/`Terminal`/`Buffer`/`Null`/`Tee` 已有；core 已可无终端 UI 跑完整 turn。 |
| Permission engine | `tools/permissions.py` | `PermissionEngine`+`Decision`，由 `Agent.permission` 用，经 `_authorize_dispatch`。**非绝对唯一咽喉**（见"关键边界 2"）。 |
| Event spine | `trace/tracer.py` + `events/*` | per-agent `wire.jsonl` 带 `id/parent_id/branch_id/turn_id`；无 session 根 `events.jsonl`（读时由 `reader.merge_session_events` 合成）。 |
| Task manager | `tasks/manager.py` | `to_state/load_state` 已有。 |

### 尚未存在或尚未成型

| 目标 | 当前状态 |
|---|---|
| Runtime 拥有 bootstrap | **缺**。`AgentRuntime.adopt()` 只接已构造 `Agent`；api-key/trust/resume/memory/`Agent(...)` 全在 `cli.main()`（~140 行，cli.py:724-863）。这是 L7-clients-共用一套-API 的真正卡点（来自 docs/09:655 的 runtime/bootstrap 设计 + CLI 目标段 + docs/11:249；**不等同 docs/09 P-1 正文**，后者主要是 Agent 解耦 refactor——compaction/message owner/UI sink）。 |
| `RuntimeThread.events()` 事件流 | **缺**。`run()` 只返回终态 `TurnResult`；in-process SDK 观测不到 turn 内进度（docs/09:671 已定义该 API）。 |
| 审批 park/respond | **缺**。仍是注入的 Python 协程回调（`confirm_fn`/`plan_approval_fn`），非可挂起的请求生命周期。 |
| `EventBus` | **缺**，且现有三套机制（Tracer/EventSink/reader）未统一。 |
| `AppServer` / JSON-RPC / SDK | **缺**（仓库内唯一 JSON-RPC 是 nanocode 作为 MCP *client*）。 |
| `SubAgentManager`/`SandboxManager`/`CapabilityRouter` | **缺**。~20 个 subagent/sandbox 编排方法直接在 `Agent`（engine.py）。 |
| CLI client 化 | `run_repl` 已建 `AgentSession`（cli.py:477）但经 `session.run_turn→agent.chat` 跑 turn，**未走 `RuntimeThread.run`**；slash command 大量触 `Agent` 私有面。 |

### 状态校准（与 09/11 同步）

```text
事件 spine（envelope/seq/merge/trace-wire）：✅ 已落地（docs/09:1334，945 tests）。
AgentSession/AgentRuntime/RuntimeThread/TurnResult/ApprovalManager：✅ 已导出（docs/11:78-85）。
故 Phase A 不是"建 runtime"，而是"把 run_repl 接上既有 runtime"。
```

---

## Layer-by-Layer 评估

### Layer 0 · Platform Adapters
现有：Anthropic/OpenAI backend、`sink.py` 包 `ui.py`、`tools/sandbox_backends/{seatbelt,bwrap}`、`paths.py`。
差距：provider backend 仍是 `Agent` mixin；shell sandbox 仍是工具细节；CLI 命令层仍有直接 print。
建议：短期不动 provider backend；先把 terminal output 朝 `EventSink` 收敛；再把 shell routing 包进 `SandboxManager`，底层 backends 保持 adapter。

### Layer 1 · Capabilities
现有：`tools/registry.py`、`tools/execute.py`、`mcp/manager.py`、`skills/`、`skills/hooks.py`、`tools/permissions.py`、`tools/run_shell.py`/`sandbox_shell.py`、subagent/`TaskManager`。
差距：无 `CapabilityRouter`；`Agent` 直接知道 MCP/skill/hook/subagent spawn/memory/sandbox routing 的大量细节；`PermissionEngine` 位置偏低（在 tools/），且**不是所有能力的不可绕过外层**。
建议优先切：`SubAgentManager`（spawn/resume/caps/artifact 写/record 状态）、`SandboxManager`（classify→host/native/microVM→escalate→结构化结果）、`CapabilityRouter`（tool/MCP/skill/hook/subagent 统一 dispatch，**全部先过 PermissionEngine**）。

### Layer 2 · Agent Core
现有：model loop（`Agent`+backend mixin）、`MessageStore` 收口、`CompressionPipeline`（每轮 budget/snip/microcompact）、`EventSink` 减耦、`_authorize_dispatch` 用 `PermissionEngine`。
差距：`Agent` 是 **2111 行**大聚合（core loop + capabilities + persistence + session/task/subagent 编排）；`AgentSession.run_turn` 仍委托 `Agent.chat`，turn lifecycle 未真正抽离。
建议：不追求小类很多；先保证 core loop 只依赖 `MessageStore`/`CompressionPipeline`/`CapabilityRouter`(经 PermissionEngine)/`EventSink`，把 subagent/sandbox/persistence 细节逐步搬出。

### Layer 3 · Event-Sourced Session Runtime
现有：`AgentSession`、`SessionContextBuilder`、per-agent wire 事件树。
**重要校准**：resume 在代码里**已是 rebuild-first/snapshot-fallback**（`restore_session` 先 `_rebuild`，注释"P5：events 为 resume 权威"；子 agent resume `prefer_events=True`），忠实才采用、否则回退 snapshot，**无数据丢失**。原稿"snapshot 为当前权威、rebuild 是未来 P5"的描述已过时。
```text
不要把本层写死为 session 根 events.jsonl —— 已锁定为 per-agent wire.jsonl + 读时 merge。
持久化 facade（SessionStore/EventLog/SnapshotStore/ArtifactStore）背后仍复用现有物理存储（flat json / v2 dir / wire.jsonl）。
context_builder.resume_messages 的 API 默认仍是 prefer_events=False；是引擎调用点 opt-in 了 events-first。
```

### Layer 4 · Runtime Facade
现有：`AgentRuntime`/`RuntimeThread`/`TurnResult`/`AgentResult`/`ApprovalManager`/`AgentSession`。
差距（核实）：
- CLI 未走 `RuntimeThread.run()`；turn 经 `session.run_turn→agent.chat`。
- **`ApprovalManager`/`RuntimeThread`/`adopt` 未进入 live CLI path**（仅测试与 `__init__` 导出引用，cli.py 内不调用）——审批由 `run_repl` 直接 `agent.set_confirm_fn`/`set_plan_approval_fn`（cli.py:485/508）注入，**不经 `ApprovalManager`**。故"approval 通过 ApprovalManager 注入"这条验收**当前未满足**。
- `adopt()` 接已构造 `Agent`，不是 `thread_start(config)`。
- approval 是 callback，不是可被协议 park/respond 的请求生命周期。
- 缺 `RuntimeThread.events()`（in-process 进度订阅）。
建议：**先让 CLI 成为 runtime client**（见 Phase A / CMD-P2.5），并补 `events()` 与 bootstrap 入口（RUNTIME-P0）。

### Layer 5 · App Server（可选 out-of-process adapter layer）
当前没有。**定位**：Layer 5 是**可选的 out-of-process adapter layer**——不是 in-process client（CLI/SDK）的必经层，也不得成为第二个 runtime；它把 L4 Runtime API 序列化到线上（见"关键边界 1"）。
目标职责：long-lived process / thread registry / request router / event fanout / approval parking / cancel routing。
禁止职责：调 `Agent` 私有方法、重实现 permission/sandbox/tool dispatch、自维护 session 状态。
**硬前置（沿用 docs/09:18/1140/1308）**：CLI 已走 Runtime + 最小 EventBus + PermissionEngine 唯一咽喉 + **出现真实的第二个 client**。

### Layer 6 · Protocol & Transports
当前没有。第一批 JSON-RPC method 只覆盖 runtime 必需面：
```text
thread.start  thread.resume  thread.list
turn.submit   turn.cancel
approval.respond
event.subscribe
task.list  task.output  subagent.list  subagent.output
session.fork
```
传输顺序：stdio JSONL → unix socket → websocket（stdio 最易测、贴近 MCP/LSP）。

### Layer 7 · Clients
当前只有 CLI。目标：CLI / Python SDK / IDE / Web UI / CI。
原则：SDK/IDE/Web/CI **不 import `Agent` 内部类**，只经 JSON-RPC 或稳定 Runtime client API。

---

## 是否符合"内部 Pi，外部 Codex"

### 内部 Pi：已到"事件 spine + rebuild 为主"阶段
已符合：per-agent event tree、`id/parent_id/branch_id/turn_id`、read-time branch chain、context builder、fork/resume seam、append-only audit。**resume 已 rebuild-first（忠实才采用 + snapshot 兜底）**。
尚未完成：compaction/snip supersession 事件契约、branch leaf cache/tree 产品视图、`rebuild == snapshot` 逐条强验收、legacy 行（无 envelope `id`）的处理。
```text
内部已 Pi 化到"事件 spine + rebuild 为主"；P5 余下的是强验收与 supersession，不是"切换本身"。
```

### 外部 Codex：有 facade，尚未成平台，且 facade 未被调用
已符合（存在性）：`AgentRuntime`/`RuntimeThread`/`TurnResult`/`ApprovalManager`、sink 分离、in-process handle。
尚未完成：facade **未被 live CLI 调用**；bootstrap 入口、`events()` 流、协议级 approval park/respond、event fanout、SDK、多 client 全缺。
```text
外部已具备 in-process runtime facade（但尚未被任何 client 调用）；
还不是 Codex-style App Server/SDK 平台。
```

---

## 关键设计边界

### 1. 一套 Runtime API，两种绑定（App Server 是适配器，不是层）

CLI 与 in-process Python SDK **直接绑 L4 Runtime**（不经 App Server / Protocol）；只有跨进程 client 才走 server + wire。

```text
        ┌─ CLI（in-process）────┐
        ├─ Python SDK（in-proc）┤──►  L4 Runtime API（唯一）
        │                       │     AgentRuntime / RuntimeThread / TurnResult /
        └─ App Server ──────────┘     ApprovalManager / events()
                 ▲
                 │ JSON-RPC over transport（序列化同一 API）
        out-of-process clients（GUI / Web / 远程 CLI）
```

不变量：**存在唯一 Runtime API；in-process 调用与序列化 RPC 只是两种绑定。** 这样"App Server 不得重实现 permission/sandbox/dispatch"成为**结构性后果**而非期望。

### 2. PermissionEngine 是入口前置门（一个例外，需显式围栏）

所有 **模型驱动** 与 **外部协议**（CLI/JSON-RPC/SDK/IDE/Web）入口都不得绕过同一 permission/capability callgate。

```text
唯一文档化例外：用户手敲的 !shell（cli.py:233 _run_user_shell）—— 等同用户自己开终端，
故意不过 PermissionEngine / allowlist / sandbox routing / is_dangerous（cli.py:237）。
它不可被模型或远端触达；App Server/RPC 路径绝不得继承此 bypass（docs/11:179/247）。
```

精度补充：
- 失败关闭的 **allowlist 仅约束子 agent**（主 agent `_allowed_tool_names is None` → 永不拦，permissions.py:386）。
- `check_permission` 对这些非 edit/shell/read 分类**没有细粒度策略**，当前多为默认 allow（fall-through）；真正能力边界分散在 `_execute_tool_call`、`_execute_agent_tool`（agent 的 depth/threads 上限、`agent` 工具剥离/backstop）、`_execute_skill_tool`（skill 约束）、MCP trust/config 与子 agent allowlist 中（注：`mcp__*` 更像真实远端工具，非 meta tool，无 `_execute_agent_tool` 那套 guard）。
- 两条硬边界（protected-path 写、`escalate=true` 逃逸到 host）**即使 bypassPermissions 也不放过**。
短期验收（修正原稿相关表述）：真实工具派发、MCP、hook shell、background run_shell、subagent/meta tool 经 PermissionEngine 或 `_execute_tool_call` allowlist 兜底；**`!shell` 作为唯一例外列明**。

### 3. 审批必须是可挂起的请求（不是 callback）

```text
in-process：confirm_fn/plan_approval_fn 协程回调即可（CLI/SDK 零回归）。
跨进程：turn 必须能在一个 ApprovalRequest(id,kind,payload) 上挂起、由外部 respond(id,decision) 唤醒。
挂起语义必须先存在于 L4 turn 内（控制流改造），不是 server 附加功能。
```
不可回归契约（docs/09:685-693/746/755）：后台/非交互请求**立即 fail-closed 拒绝**（不挂起等 client）；按 **agent_id 帧**挂起而非整 turn；cancel 经 `agent.abort()` 唤醒**各深度**所有 pending、且 `await` 后读 `_aborted`；按身份 dedup。

### 4. 单一 RuntimeEvent 流 + sinks/projections（Pi-aligned）

现有三套（Tracer / EventSink / reader）中，写侧两套**结构不兼容**：`EventSink` 是 12 个语义 UI 方法、fire-and-forget、无持久化；`Tracer.emit(type,**fields)` 写持久 `wire.jsonl`。

目标**不是**「Tracer 接管 EventSink」（那会把 Tracer 概念上抬成业务中心），而是 **Agent/Runtime core 只发一条 `RuntimeEvent` 流**，由 dispatcher fan-out 到多个 sink/projection：

```text
Agent core ── emit RuntimeEvent ──►
    ├─ WireSink / TracerSink        （durable：写 wire.jsonl；过渡期复用现有 Tracer.emit）
    ├─ EventSinkProjection           （UI：渲染终端）
    ├─ Buffer subscriber             （capture：TurnResult.final_response）
    ├─ RuntimeThread.events()        （in-process subscription：SDK）
    └─ future JSON-RPC notification  （protocol projection：App Server）
```
原则（Pi 证明）：core event stream 才是核心，EventBus/dispatcher **只做 fanout**；session/wire/UI **互不调用**。`Tracer.emit` 仅作过渡实现入口，目标命名是 `RuntimeEvent`/`EventStream`/`EventDispatcher`。
分类（durable/ephemeral）是**静态、按 type 字符串的表**（DURABLE_TYPES/EPHEMERAL_UI_TYPES），绝不作 emit payload 上的 kwarg（否则 `ui=` 折进 `SessionEvent.data` 污染 wire）。
验收：没有事件经两套词汇发两遍；每个 sink 都是同一条流的投影；flag-ON wire == flag-OFF wire（逐行逐 key）。
实施保守两步：① 引入薄 `RuntimeEvent`/`EventDispatcher` + 静态表 + parity 测试（不接调用点）；② 逐类把 `self._sink.* + tracer.emit(...)` 改成一次 `dispatch(RuntimeEvent)`，每类一个 parity gate。

### 5. 外部 API 不暴露内部 event tree 细节
外部用产品语义（`thread/turn/session/task/approval`）；`wire event id/parent_id/branch_id/leaf_id` 是内部状态机语义，只作高级调试字段或 fork anchor。

### 6. P-1 不等于停止每轮压缩
`budget/snip/microcompact` 必须保留每次 provider call 前执行（已核实：pipeline 每轮 `while True` 顶部跑）。P-1 真实目标是压缩策略不散落在 provider loop，由 `CompressionPipeline`/context owner 统一准备。`rebuild==snapshot` 强验收属 Phase F。

### 7. 先不要拆发行包
`Agent` 仍是 live path 大聚合、App Server/SDK 无真实 caller —— 过早移文件只制造 import churn。先在单包内形成逻辑边界，再移目录。

---

## 推荐路线图

> **Phase A 直接沿用 docs/11 的 CMD-P0..CMD-P2.5，不重述。** 其不变量（most-specific-first、未知 `/foo`→chat、全角 `／` 归一、复用现有 `AgentSession`、runner 失败隔离）与 **characterization 套件 merge gate** 全部适用。

### Phase A · CLI client 化（== docs/11 CMD-P0→P1→P2→P2.5）
PR 顺序（**已修正——原稿把 P2.5 排在抽取前，反了依赖**）：
```text
CMD-P0  纯抽取 dispatch → commands/registry+runner（characterization 双侧绿为 merge gate；types.py 已落地）
CMD-P1  补全 + /help 从同一 registry（删 _BUILTIN_COMMANDS 与 --help 两份漂移副本）
CMD-P2  /trace 复用 trace_cmd.run，隔离 SystemExit + shlex ValueError
CMD-P2.5 普通 chat / skill turn 改走 RuntimeThread.run（接 TurnResult + cancel/_aborted/approval）
```
依赖原因：CMD-P2.5 要把 registry 的 `Prompt` variant 接进 `RuntimeThread.run`，该 variant 在 CMD-P0 抽取前不存在。
验收：普通输入不直接调 `agent.chat`；cancel/Ctrl-C 不回归；未知 `/foo` 仍 fallthrough；`/help`/补全/dispatch 来自同一 registry；**characterization 套件在抽取 PR 两侧不变**；提供 feature flag（如 `NANOCODE_REPL_VIA_RUNTIME`）以便 P2.5 cutover 回退。
**并覆盖 headless 一发式 `-p` 路径**（cli.py:888-895 直接 `agent.chat`，不进 run_repl）：或同样走 `RuntimeThread.run`，或显式标注为交互式专属。

### Phase B · Capabilities 边界
`CAP-P1 SubAgentManager` / `CAP-P2 SandboxManager` / `CAP-P3 CapabilityRouter`（统一 dispatch，全部过 PermissionEngine）。
依赖注意：`SubAgentManager` 的 artifact 写**依赖持久化路径**（engine.py:1095/1104/1284 现手拼 `agent_wire_path`/`task_dir`/`result.md`）。要么先落薄 `ArtifactStore` seam，要么明确 CAP-P1 只消费 `session/v2.py` helper（不拼裸字符串），并注明 PERSIST-P1 会 re-home。
验收：`Agent` 不再独担 subagent artifact 写/task 状态流转/sandbox route 全部细节；background shell/hook/MCP/skill/subagent 无绕过 PermissionEngine 的测试。

### RUNTIME-P0 · Runtime 拥有 bootstrap（新增，L7-共用-API 的真正卡点）
`AgentRuntime.create_session(config)/thread_start(config)` 内化 `cli.main()` 的构造（load_config / 按 cwd 复检 trust / resume-先于构造 / memory-backend / MCP 于 runtime 层，docs/09:655/683/909）。`adopt(existing_agent)` 降为 legacy shim。**没有它，SDK/AppServer 无法共用一套入口。**

### Phase C · Persistence facade
`SessionStore/EventLog/SnapshotStore/ArtifactStore/TaskRecordStore/SubAgentRecordStore`，物理实现暂复用（flat json / v2 dir / wire.jsonl / state.json / artifacts）。
验收：上层不直接拼 session/artifact/wire 路径；rebuild 不忠实时明确 fallback snapshot；不误增 session 根 `events.jsonl`。

### RUNTIME-P2 · 审批即可 park 的请求（新增）
把 `confirm_fn/plan_approval_fn` 换成 `ApprovalRequest(id,kind,payload)`，turn 可挂起；in-process resolver（保留 CLI 行为零回归）+ 外部 resolver seam。保住"关键边界 3"的全部契约。**这是判据 #6 的真正前置**——不做它，JSON-RPC 再全也无法 respond approval。

### Phase D · 事件统一 + EventBus + App Server
`EVENT-P1` 单一 RuntimeEvent 流 + sinks/projections（durable→Tracer 写 wire；EventSinkProjection 渲染 UI；逐类迁移 + parity gate）→ `EVENT-P2` 最小 `EventBus.emit/subscribe`（含 `RuntimeThread.events()` in-process 订阅，docs/09:671）→ `SERVER-P1` stdio App Server（thread registry/router/fanout/approval parking）。
**App Server 硬前置：出现真实第二个 client（docs/09:18/1140/1308）；`EVENT-P2` 是最后一个无条件步骤。**
验收：headless in-process 测试可订阅 turn/tool/task 事件；AppServer 不调 `Agent` 私有；CLI 与 server 共用 Runtime。

### Phase E · Protocol & SDK
JSON-RPC method 定义 / stdio JSONL transport / Python SDK client（不 import `Agent`）/ 后续 unix socket/websocket。
验收：外部进程可 `thread.start`/`turn.submit`/收事件/`approval.respond`；protocol 错误/取消/审批超时语义明确。

### Phase F · Pi-style P5 完成
compaction supersession event / branch leaf cache / `rebuild==snapshot` 逐条等价 / fork tree 产品面 / session inspect。
验收：触发 budget+snip+microcompact+full compact 后 rebuild 与模型实际所见等价；fork 不覆盖原分支；snapshot 降为 cache 的切换有开关与回退。

---

## 建议目录演进

短期保持现状（`agent/ tools/ tasks/ skills/ mcp/ subagents/ session/ events/ trace/ entrypoints/`）。
当 Phase A/B/C 稳定后，逐步演进为 `core/`（runtime/thread/session/messages/compression/permissions）、`capabilities/`（router/tools/mcp/skills/hooks/sandbox/subagents/tasks）、`persistence/`、`protocol/`、`server/`、`cli/`。
**拆成 `nanocode-core/platform/cli` 仅当**：① CLI 已完全经 Runtime 跑普通 turn；② App Server/SDK 有真实 caller；③ Capabilities 有稳定 facade；④ Persistence facade 已隔离物理布局；⑤ 内部模块不反向 import CLI/UI。

---

## 风险与控制

| 风险 | 控制方式 |
|---|---|
| 过早拆目录 import churn | 先做 facade + 依赖方向，目录后移。 |
| CLI 与 App Server 两套 runtime | 先让 CLI 走同一 L4 Runtime；App Server 是其传输适配器（边界 1）。 |
| 外部入口绕过权限 | PermissionEngine/CapabilityRouter 唯一 dispatch 门；`!shell` 是显式围栏的本地用户例外，远端不得继承（边界 2）。 |
| events 被过早当 resume 事实源 | 已 rebuild-first + snapshot 兜底；P5 做 `rebuild==snapshot` 强验收后才移除 snapshot。 |
| EventBus 变第二业务中心 | 单一 RuntimeEvent 流；core 发事件，dispatcher/EventBus 只 fanout，sinks 互不调用（边界 4）。 |
| 审批无法跨进程 | RUNTIME-P2 先在 turn 内做 park/respond，保后台 fail-closed/逐帧/cancel 唤醒契约（边界 3）。 |
| JSON-RPC 暴露内部细节 | 外部用 thread/turn/task/session 语义，event id 只作 anchor/debug（边界 5）。 |
| CLI→runtime cutover 回归 | characterization 套件双侧绿 + feature flag 可回退。 |
| 编号/层号跨文混淆 | 用本文 crosswalk 表；裸 `P` token 不跨文比较。 |

---

## 最小下一步

```text
落地 docs/11 CMD-P0（registry+runner 抽取，characterization 双侧绿），
再 CMD-P1→P2→P2.5 让普通 turn 经 RuntimeThread.run。
```
完成后 nanocode 第一次真正证明：**CLI 只是一个 client；AgentRuntime 才是统一入口；AgentSession/AgentCore 才是内部执行层。**

推荐 PR 顺序（已修正排序，见 crosswalk）：
```text
1. CMD-P0     registry + runner 抽取（characterization gate）
2. CMD-P1     补全 + /help 单源
3. CMD-P2     /trace SystemExit/ValueError 隔离
4. CMD-P2.5   普通 chat / skill turn 走 RuntimeThread.run（+ headless -p 路径）
5. RUNTIME-P0 create_session(config) 拥有 bootstrap
6. CAP-P1     SubAgentManager（持久化契约见 Phase B）
7. CAP-P2     SandboxManager
8. PERSIST-P1 SessionStore/EventLog/ArtifactStore facade
9. RUNTIME-P2 审批 park/respond（in-process resolver 先行）
10. EVENT-P1  单一 RuntimeEvent 流 + sinks/projections（逐类迁移，parity gate）
11. EVENT-P2  最小 EventBus + RuntimeThread.events()
12. SERVER-P1 stdio App Server + 第一批 JSON-RPC（前置：第二个 client）
```

---

## 成功判据

nanocode 进入"可嵌入式 agent runtime"门槛，当且仅当：
```text
1. CLI（含 -p 一发式）不直接调用 Agent 私有方法跑 turn。
2. RuntimeThread.run 返回稳定 TurnResult。
3. 所有工具/能力派发都过 PermissionEngine/CapabilityRouter（!shell 本地例外除外）。
4. headless test 可无 terminal UI 跑完整 turn。
5. runtime events 可在 RuntimeThread 层订阅，并能同时写 persistence 与 fanout 到 client。
6. 外部进程可经 JSON-RPC submit turn / receive events / respond approval。
7. session resume/fork 的上下文来源、fallback、审计路径明确。
```

当前状态（已按代码核实校准）：
```text
已满足（单元级）：2（TurnResult 完整稳定）、4（NullSink/BufferSink 可跑 headless，唯尚未接测试路径 → 能力具备未验证）
大部分满足：3（_authorize_dispatch 过 PermissionEngine；CapabilityRouter 未建；!shell 例外）
部分满足：1（turn 经 session.run_turn，但 slash command 仍触私有面 + RuntimeThread/ApprovalManager 未被调用 + 一发式 -p 直调 agent.chat）
           5（wire.jsonl 持久化在，但无 RuntimeThread.events()/EventBus/fanout）
           7（resume/fork + snapshot 兜底在，且已 rebuild-first；rebuild==snapshot 强验收未做）
尚未满足：6（无 JSON-RPC / App Server / SDK）
```

最终判断：
```text
nanocode 已走在"内部 Pi + 外部 Codex"的正确方向，底座（事件 spine + runtime/session 类）已落地；
但 facade 尚未被任何 client 调用，仍是单包内过渡实现，不是完整可嵌入平台。
下一阶段优先：CLI client 化（沿用 docs/11）+ Runtime 拥有 bootstrap + capability/persistence 边界，
再进入审批 park/respond → 事件统一/EventBus → App Server / JSON-RPC / SDK。
```
