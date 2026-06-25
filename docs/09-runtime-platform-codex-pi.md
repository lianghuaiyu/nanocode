# nanocode Runtime 平台化改造报告：Codex 外部接口 + Pi 事件化内核

日期：2026-06-08

范围：nanocode 当前 Python 实现、OpenAI Codex runtime/App Server/SDK 设计、Pi coding agent 的事件化 session runtime。

目标：给 nanocode 后续改造成”可嵌入 agent 平台”提供一份可以直接落地的架构报告。本文不要求一次性实现完整 App Server、SDK、session tree UI，而是定义清晰分层、迁移顺序和关键边界。

## 修订记录（2026-06-08，对照代码复核后）

本文初版把 nanocode 当成”尚无事件系统”来设计；对照 `src/nanocode/` 复核后做了如下修订（不改变 runtime-first 的总方向，只补承重盲点和一处过时前提）：

1. **不再 greenfield 新建事件内核**：仓库已存在 `trace/tracer.py`（`Tracer.emit` + `JsonlSink` + `SCHEMA_VERSION`/`seq`/`session_id`/`parent_session_id`/`child()`）和**每个 agent always-on 的 `agents/<id>/wire.jsonl`**，以及 `nanocode trace` 的 replay/summary 视图。新增「现有事件基础设施对账」一节，P0/P1 改为”promote 现有 tracer/wire 为 `SessionEventStore`”而非并列第三条 lane。
2. **更正”events 是事实源、snapshot 是 cache”为分阶段命题**：当前模型循环每轮**原地销毁/改写**消息列表（compaction 整体替换、snip/budget/microcompact 就地改写 tool_result），故”从 append-only 事件重建上下文”在 compaction/snip 事件化之前不成立。迁移期 resume 仍以 snapshot 为准（见 Risk 2 重写 + 持久化兼容策略 + ContextBuilder 段）。
3. **多处”待建”实为”已存在，应形式化/扩展”**：`AgentResult` + bounded envelope、`SubAgentRecord`、结构化 `plan_approval_fn`、`context=isolated`、`compaction` 事件均已落地；相关段落改为”扩展而非替换”，并点名不可回归的不变量（宿主派生 `files_read/files_modified`、按身份 dedupe、后台 fail-closed 等）。
4. **补齐被低估的契约**：取消/中断语义、approval 作为协议事件与前台子 agent 嵌套重入、多进程单 session 并发与原子写、耐久性/恢复、events.jsonl 的 schema 演进——这些一旦协议冻结错就是 breaking change，本次先把契约写进文档。
5. **更正过时安全前提**：原生 OS sandbox（seatbelt/bwrap）已落地并经多轮 hardening（`2ce047d`+），**P4 子 agent call-time 工具限制为 fail-closed 强制**（`engine.py:687` + `tests/agent/test_subagent_callgate.py`），并非 advisory。”先关安全欠债再平台化”由阻断级降级为”任何外部入口必须复用同一 fail-closed 闸”。
6. **范围与排期**：App Server / JSON-RPC / SDK / 扩展生态当前**无消费者**，标注为 aspirational，gating 在”出现第二个 client”之上；`PermissionEngine` 合并升为早期编号阶段；MVP 收窄为”仅事件统一”。

## 资料来源

- OpenAI: [Unlocking the Codex harness: how we built the App Server](https://openai.com/index/unlocking-the-codex-harness/)
- OpenAI Codex manual: Codex App Server、SDK、subagents、hooks、sandbox、plugins 相关章节
- OpenAI Codex source: [codex-rs/app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- Pi docs: [Session File Format](https://pi.dev/docs/latest/session-format)
- Pi docs: [SDK](https://pi.dev/docs/latest/sdk)
- Pi docs: [RPC Mode](https://pi.dev/docs/latest/rpc)
- Pi docs: [Extensions](https://pi.dev/docs/latest/extensions)

## 总体判断

Codex 和 Pi 代表了同一代 agent runtime 平台化思路的两个侧面：

- **Codex 强在外部运行时接口**：同一套 Codex harness 被 CLI、IDE、App、Web、SDK 复用；App Server 通过双向 JSON-RPC over stdio 暴露 thread/turn/item、事件流、审批和持久化。
- **Pi 强在内部事件化状态模型**：session 是 JSONL tree，每个 entry 通过 `id` / `parentId` 组成可分支图；`AgentSessionRuntime` 负责 new/resume/fork/import；extensions 可以监听和参与 session、tool、context、compact、tree 等生命周期。

nanocode 最适合走一条混合路线：

```text
外部像 Codex：
  App Server / JSON-RPC / SDK / CLI client / approval protocol

内部像 Pi：
  event-sourced AgentSession / append-only JSONL / session tree / context builder / lifecycle hooks
```

这条路线的核心不是“增加更多功能”，而是把现有 CLI-first 实现改造成 runtime-first：

```text
Agent core 只负责模型循环；
AgentSession 负责事件化会话状态；
AgentRuntime 负责 thread/turn 生命周期；
App Server 负责协议；
CLI/SDK/IDE 只是 client。
```

## nanocode 当前状态简析

当前 nanocode 已经具备很多平台化雏形：

- `src/nanocode/agent/engine.py` 是主 agent loop，已经能管理 OpenAI/Anthropic backend、工具执行、skills、subagents、tasks、memory、hooks、sandbox。
- `src/nanocode/tools/registry.py` 和 `src/nanocode/tools/execute.py` 提供工具表和分发。
- `src/nanocode/subagents/config.py` 已支持自定义 agent、YAML frontmatter、`extends`、`tools` / `allowed-tools`、`disallowed-tools`、`model`、`max-turns`、`timeout-ms`。
- `src/nanocode/tasks/models.py` 和 `src/nanocode/tasks/manager.py` 已能表达后台任务和子 agent record。
- `src/nanocode/session/v2.py` 已有 session 目录结构：`state.json`、`main/messages.json`、`agents/<id>/messages.json`、`tasks/<id>/`。
- `src/nanocode/skills/hooks.py` 和 `engine.py` 里已有工具级 hooks 雏形。
- **已存在一套事件设施**：`src/nanocode/trace/`（`Tracer.emit(type, **fields)` → 可插拔 `Sink` Protocol → `JsonlSink`），带 `SCHEMA_VERSION`、逐事件 `seq`、UTC `ts`、`session_id`/`parent_session_id` 链接、`Tracer.child()` 给子 agent；**每个 agent 都有一条 always-on 的 `agents/<id>/wire.jsonl`**（`engine.py:286`，不受 trace 开关控制）+ 可继承的 debug sink；`nanocode trace` 已能 replay/summary（`trace_cmd.py` + `report.py`）。已 emit 的类型见下节对账表。
- 原生 OS sandbox（seatbelt/bwrap 两层 confinement，`2ce047d`+多轮 hardening）、权限、protected roots、background fail-closed、**P4 子 agent call-time 工具限制（fail-closed 强制，`engine.py:687`）** 等安全能力已落地。

但这些能力仍然偏 CLI-first 和 snapshot-first：

- CLI 直接创建并驱动 `Agent`，`src/nanocode/entrypoints/cli.py` 仍承担大量 runtime wiring。
- backend 直接调用 terminal UI，例如 `print_tool_call` / `print_tool_result`；`Agent._emit_text` 还按 `is_sub_agent` 分叉 print/buffer——core 知道 UI。
- 审批是**两个**本地回调（`confirm_fn` 返回 bool + `plan_approval_fn` 返回结构化 dict），都不是可跨 client、可挂起 turn 的审批协议。
- session v2 注释写”事件流”，实际更接近 JSON snapshot；且其快照写入是**有条件**的（仅 v2 会话或有 fork 子 agent 时，见持久化兼容策略），而 `wire.jsonl` 始终写。
- 事实源分散在**三条 JSONL lane**（per-agent `wire.jsonl`、可选 `traces/*.jsonl`、以及本文将新增的 `events.jsonl`）+ snapshot + UI 输出之间，缺统一事实源——本文必须先对账（见下节），否则会亲手制造它想消灭的碎片化。
- subagent 是 `TaskRecord + Agent(messages.json)`，已是 context-isolated，但还不是 session branch/thread（缺事件级 `id`/`parent_id` 树链接）。
- 外部程序无法通过稳定 API 启动 thread、run turn、订阅事件、响应 approval。

## Codex 对 nanocode 的启发

Codex App Server 的关键抽象有三个：

1. **Thread**：持久会话容器，可以 create/resume/fork/archive。
2. **Turn**：一次用户输入触发的一轮 agent 工作。
3. **Item**：turn 内的原子输入/输出单元，例如 user message、assistant message、tool execution、approval request、diff、artifact。

Codex App Server 进程包含：

- stdio reader
- message processor
- thread manager
- core threads

一个 client request 可以产生多个 server notifications。server 也可以主动发起 approval request，并暂停 turn 等 client 回复。这是普通 request/response API 无法表达 agent loop 的原因。

对 nanocode 的启发：

- CLI 不应继续是唯一产品本体，而应变成 App Server 或 in-process runtime client。
- 外部接口应该围绕 `thread.start`、`thread.resume`、`turn.run`、`approval.respond`、`task.stop` 等稳定方法。
- UI 不能依赖 core print；core 应该 emit typed event，client 自己渲染。
- approval 是协议事件，不是本地 stdin prompt。
- SDK 应该包协议，不应直接依赖 `Agent` 内部消息结构。

## Pi 对 nanocode 的启发

Pi 的关键不是外部协议，而是内部 session runtime：

- session 文件是 JSONL。
- entries 通过 `id` / `parentId` 组成树。
- `/tree`、`/fork`、`/clone`、branch summary 都建立在这个 session graph 上。
- context builder 从当前 leaf 往 root 构造模型上下文。
- `AgentSessionRuntime` 管理 new/resume/fork/import 等 session replacement。
- `AgentSession` 管 prompt/steer/followUp/subscribe/compact/model 切换。
- extension runtime 可以订阅 session、resource、agent、tool、context、provider、compact、tree 等事件。
- RPC mode 支持 headless JSON protocol over stdin/stdout。

对 nanocode 的启发：

- `events.jsonl` 应该是 session source of truth，而不是只作为调试日志。
- `state.json`、`messages.json` 应该是 snapshot/cache，不是唯一事实。
- fork 不应复制一份 messages，而应从某个 event id 创建新 branch leaf。
- subagent 也应该可以是 child branch/thread，而不是普通后台 task。
- context 构建应由 `SessionContextBuilder` 从 event tree 生成，而不是散落在 backend messages list 中。
- hooks/extensions 应基于 lifecycle events，而不是每个功能自己定义一套入口。

## 目标架构

推荐分层如下：

```text
Layer 7  Clients
         CLI / Python SDK / IDE / Web UI / CI scripts

Layer 6  Protocol & Transports
         JSON-RPC method schema / stdio JSONL / unix socket / websocket

Layer 5  App Server
         long-lived process / request router / thread manager / event fanout

Layer 4  Runtime Facade
         AgentRuntime / RuntimeThread
         对外暴露 thread.start, turn.run, approval.respond, task.stop

Layer 3  Event-Sourced Session Runtime
         AgentSession / SessionEventStore / SessionContextBuilder
         BranchManager / ArtifactStore

Layer 2  Agent Core
         model loop / tool scheduling / compaction / subagent orchestration

Layer 1  Capabilities
         tools / MCP / skills / hooks / permissions / sandbox / subagents

Layer 0  Adapters
         OpenAI / Anthropic / filesystem / shell sandbox / terminal UI renderer
```

依赖方向必须单向：上层调用下层，下层不能 import 上层。

最重要的边界：

- `Agent Core` 不能 import `rich`、`prompt_toolkit`、CLI confirm prompt。
- `Session Runtime` 不能依赖具体 UI。
- `Protocol` 不能暴露 provider-specific message list 作为稳定 API。
- `Capabilities` 必须统一走 permission/capability，不允许 tool、hook、subagent 各自绕路。
- `Persistence` 最终以 `events.jsonl` 为事实源、snapshot 为恢复 cache；但**这是迁移终态而非起点**——在 compaction/snip 事件化且 rebuild 通过 byte-equal 验收前，resume 仍以 snapshot 为权威（见 ContextBuilder 段与 Risk 2）。且该事实源应由现有 tracer/wire promote 而来，不另起 lane。

## 建议目录结构

目标目录可以逐步演化成（注意：`events/` 与 `session/event_store.py` 是对现有 `trace/`（`tracer.py`/`sinks.py`）的**提升与扩展**，不是平行新建——`events/bus.py` 应让现有 `Tracer.emit` 调用点 fan-in，详见「现有事件源对账」）：

```text
src/nanocode/
  runtime/
    __init__.py
    config.py             # RuntimeConfig / ThreadConfig
    runtime.py            # AgentRuntime
    thread.py             # RuntimeThread
    approvals.py          # ApprovalRequest / ApprovalManager
    results.py            # TurnResult / AgentResult / ArtifactRef

  events/
    __init__.py
    models.py             # SessionEvent / RuntimeNotification
    bus.py                # EventBus
    projection.py         # session event -> runtime notification

  session/
    event_store.py        # append-only events.jsonl
    context_builder.py    # leaf -> model messages
    branches.py           # branch/leaf/fork
    artifacts.py          # result/log/artifact paths
    snapshots.py          # state/messages snapshot cache
    v2.py                 # existing compatibility layer

  protocol/
    methods.py            # JSON-RPC method names
    dispatcher.py         # request -> runtime call
    errors.py
    types.py

  server/
    app_server.py
    transports/
      stdio.py
      unix_socket.py
      websocket.py

  sdk/
    client.py
    thread.py

  agent/
    engine.py             # gradually slimmed core loop
    openai_backend.py
    anthropic_backend.py
    compaction.py
    models.py
```

第一阶段不必重排所有现有文件。可以先新增 runtime/events/session 子模块，然后让现有 `Tracer.emit` 调用点 fan-in 到统一 event store（单写 fan-out，非并行双写——见下节对账与 P1）。

## 现有事件基础设施对账（P0 前置，必须先读）

> 本节是初版最大的盲点修订。本文的"事件化内核"不是从零开始——`trace/tracer.py` 已经实现了 `SessionEventStore` 八成的机制。在写任何 `events/bus.py` / `session/event_store.py` 之前，必须先就下面三条 lane 做出**明确的书面取舍**，否则会亲手制造本文宣称要消灭的"缺统一事实源"（行64）。

### 三条 lane 的现状

| lane | 路径 | 是否 always-on | 现状 / 去向 |
|------|------|----------------|------|
| per-agent wire | `~/.nanocode/sessions/<sid>/agents/<id>/wire.jsonl` | **是**（不受 trace 开关控制，`engine.py:286`） | 每 agent 一个，append-only，崩溃隔离，已有 `seq`/`ts`/`session_id`/`parent_session_id`。**→ 原地升级为事实源 entry tree**（补 `id`/`parent_id`/`turn_id`/`branch_id`/`agent_id`） |
| debug trace | `./.nanocode/traces/<sid>.jsonl` | 否（需开启） | 调试用，可被子 agent 继承。**→ 降级为纯读端/可选 debug**，不作事实源 |
| ~~session 根 events~~ | ~~`sessions/<sid>/events.jsonl`~~ | — | **已否决**：不新增此文件；统一流由读时 merge 生成（见下） |

`Tracer.emit` 已具备：`v=SCHEMA_VERSION`、逐事件 `seq`、UTC `ts`、`session_id` + `parent_session_id`、`Tracer.child()` 子 agent 链接、`JsonlSink` 的 lazy-open + I/O 故障自禁用 + close finalize（`engine.py:426-432` 的 finally 已专门处理错误/超时/取消路径的句柄泄漏）。`SessionEvent` 相对它**只多了**：全局唯一 event id（现 `seq` 仅单 agent 内唯一）、`branch_id`、`turn_id`、事件级 `parent_id`（事件 DAG 父，区别于会话级 `parent_session_id`）。

### 决策：promote 现有 wire，不另起 lane（已锁定，对标 Pi）

参照 Pi 的 session 模型（[session-format](https://pi.dev/docs/latest/session-format)：一个 session 一条 JSONL = 唯一事实源、`{id,parentId}` 成树、`buildSessionContext()` 从 leaf 走到 root、compaction **追加** `CompactionEntry` 不改写、跨 session fork 用 header 的 `parentSession` 指针），nanocode 采用其 per-agent 等价形态。**已锁定的 5 + 3 条决定**：

1. **事实源 = per-agent `wire.jsonl` 原地升级为 Pi-style entry tree**。不新建结构性独立的第二 emitter，给现有 `Tracer.emit` 事件 dict 补树链接字段即可（已有 `assistant_message.tool_uses`、`tool_call/tool_result.tool_use_id`，配对本就结构化）。
2. **不新增 session 根 `events.jsonl`**；跨 agent 的统一时间线由**读时 merge/projection** 生成（复用 `trace/report.py` 既有的 `load_session_events`/`_depths` 合并+depth 逻辑，改指向 `agents/*/wire.jsonl`）。这与 Pi「子 agent/fork 是独立文件、靠 `parentSession` 链接、`/tree` 导航结构而非拍平流」一致，且天然得到「每 agent 单写者、无并发写冲突」。
3. **旧 session 不迁移**；只从新 turn 起写新 schema。混合 schema 文件（前段 legacy flat、后段带树字段）由 reader 容忍：legacy 行**参与审计展示、不参与 tree rebuild**。
4. **snapshot 仍是 resume 权威**，直到 P5 落地 compaction supersession + rebuild 验收。
5. **第一刀只做事件 spine**：不碰 PermissionEngine(P0.5)、P-1 解耦、AgentSession/App Server。

on-disk schema（锁盘格式）：

6. **保留现有 type 名**（`turn_end`/`tool_call`/`assistant_message`/...），**不**做 canonical rename（`turn_end→turn_completed` 那套词表是后续单独 bump `SCHEMA_VERSION` + 改 `report.py._SUMMARIZERS` 的改动，不在本刀）。
7. **event id = `evt_{agent_id}_{seq}`**（会话内唯一、无 RNG、可复现；比 Pi 的随机 8-hex 更利于确定性测试）。`parent_id` 默认链同一 agent 上一条 entry 的 id；子 agent/fork 预留跨 agent `parent_event_id`（指向父分支的 fork 点，对标 Pi 的 `parentSession`+fork entryId）。
8. **`seq` 必须从现有 wire 的 tail/max 初始化**（resume-safe）：`wire.jsonl` 是跨 resume 的 append 文件，而 `Tracer._seq` 每次构造重置为 0，若不从 tail 续号，`evt_{agent_id}_{seq}` 会与上一轮 id 碰撞。`_build_tracer` 在挂 wire sink 前先读该文件的 max seq + 1 作为 start_seq。

### merge / 排序约定（锁定）

- **审计展示序** = `(ts, agent_id, seq, line_no)`——稳定、可复现的展示顺序。
- **因果序**只看 `parent_id`，且只在单 agent/单分支内成立；**跨兄弟 agent 不承诺全序**（Pi 同样不维护跨文件全序）。

### 旧的「session 根 + fan-in」方案（已否决）

> 早期草案曾写「session 根放一个 `events.jsonl`、per-agent sink 同时 fan-in」。已否决：它引入第二个写者 + 跨 agent 单文件并发写，违背决定 2 的单写者性质。统一流改为纯读时 merge。

### 事件词表映射（现有 → 提议）

提议的事件名（`turn_completed`/`tool_call_started`/...）与代码**已 emit 的名字不一致**，照搬会 fork 出双词表。采用下表把现有名提升为 durable schema（沿用现有字符串作为 wire schema，因为 `trace/report.py` 的 `_SUMMARIZERS` 硬编码读它们；改名须同步 bump `SCHEMA_VERSION` 并改 `report.py`）：

| 现有 emit（代码） | 提议 durable type | 备注 |
|-------------------|-------------------|------|
| `session_start` | `session_started` | |
| `user_message` | `message_appended` (role=user) | |
| `assistant_message` | `message_appended` (role=assistant) | 须带 tool_use block（见数据模型） |
| `tool_call` | `tool_call_started` | 带 `tool_use_id` |
| `tool_result` | `tool_call_completed` / `_failed` | 带 `tool_use_id`，1:1 配对 |
| `permission_decision` | `permission_resolved` | |
| `compaction` | `compaction_completed` | 须带被取代区间（见 ContextBuilder 段） |
| `turn_end` | `turn_completed` | 已带 `input_tokens`/`output_tokens`/`turns` |
| `session_end` | `session_archived` / 复用 | |
| `tool_blocked` | 保留 | P4 fail-closed 的审计事件 |
| `budget_exceeded` | 保留 | |
| `llm_request` / `llm_response` | 保留为 runtime-only 或并入 turn 生命周期 | 不必持久化为事实源 |

> 当前 `budget`/`snip`/`microcompact` 三个上下文裁剪 tier **完全没有事件**——这与"events.jsonl 是事实源"直接冲突（上下文被改写却无事件痕迹）。补事件的方案见 ContextBuilder 段。

## 核心数据模型

### SessionEvent

`SessionEvent` 是内部事实，不只是 UI 通知。它是现有 tracer 事件 dict 的**超集**（复用 `v`/`seq`/`ts`/`session_id`，新增树链接字段）。

```python
@dataclass
class SessionEvent:
    v: int                       # schema version，沿用 tracer 的 SCHEMA_VERSION，勿重置
    id: str                      # = f"evt_{agent_id}_{seq}"，会话内唯一、可复现、无 RNG
    session_id: str
    agent_id: str                # 由现有 artifact_id 派生（main / agent-001 ...）
    type: str                    # 锁盘期沿用现有名（turn_end/tool_call/...），不 canonical rename
    timestamp: str
    seq: int                     # 单 agent 内单调；resume 时须从 wire tail/max 续号（见对账决定 8）
    parent_id: str | None = None # 同 agent 上一条 entry 的 id（事件 DAG 父）
    parent_event_id: str | None = None  # 子 agent/fork：父分支的 fork 点（对标 Pi parentSession+entryId）
    branch_id: str = "main"
    turn_id: str | None = None
    data: dict = field(default_factory=dict)
```

> id/parent/turn 的赋值放在 `Tracer.emit` 现有 try/except **内**，保住「instrumentation 绝不影响 agent」——链接逻辑出 bug 也不能让 turn 崩。legacy flat 行（无 `id`/`parent_id`）由 reader 容忍：参与审计展示、不参与 tree rebuild。

**数据模型关键修订：事件须能表达带工具的消息**。好消息：代码现状已满足——`assistant_message` 事件已带 `tool_uses=[{id,name,input}]`（`openai_backend:158`/`anthropic_backend:214`），`tool_call`/`tool_result` 已带 `tool_use_id`（`openai:203/237/252`、`anthropic:251/259/280`），配对本就是结构性的、按 id 而非位置。

**锁盘期 on-disk 形态保持 flat-additive（重要）**：第一刀**只在现有扁平事件 dict 上增加 envelope 字段**（`id`/`parent_id`/`turn_id`/`branch_id`/`agent_id`），**不把 payload 收进嵌套 `data`**。原因：`trace/report.py._SUMMARIZERS` 直接读顶层键（`e.get("tool")`、`e["messages"]`、`e.get("tool_uses")`...），收进 `data` 会破坏 `nanocode trace`。`SessionEvent.data` 是**读侧派生视图**（reader 把非 envelope 顶层键归集为 `data`），不是盘上结构。把 payload 规整进嵌套 `data` 与 canonical rename 一样，是后续 bump `SCHEMA_VERSION` 的独立改动。

最小 JSONL 形态（flat-additive，envelope 字段 + 现有扁平 payload）：

```json
{
  "v": 1,
  "id": "evt_main_122",
  "session_id": "sess_abcd",
  "agent_id": "main",
  "branch_id": "main",
  "turn_id": "turn_3",
  "seq": 122,
  "parent_id": "evt_main_121",
  "parent_session_id": null,
  "type": "tool_call",
  "ts": "2026-06-08T10:00:00Z",
  "tool": "grep_search",
  "input": {"pattern": "ERROR"},
  "tool_use_id": "toolu_01..."
}
```

### RuntimeNotification

`RuntimeNotification` 是给 client 的投影。不是所有 runtime notification 都需要持久化。

```python
@dataclass
class RuntimeNotification:
    id: str
    session_id: str
    thread_id: str
    type: str
    data: dict
    turn_id: str | None = None
    client_id: str | None = None
```

例子：

- token delta 可以只作为 `RuntimeNotification`。
- 最终 assistant message 必须写入 `SessionEvent`。
- spinner/status message 可以只通知 UI。
- artifact 写入必须写入 `SessionEvent`。

### TurnResult

```python
@dataclass
class TurnResult:
    session_id: str
    thread_id: str
    turn_id: str
    status: Literal["completed", "failed", "cancelled", "timed_out"]
    final_response: str
    input_tokens: int
    output_tokens: int
    artifacts: list[ArtifactRef]
    error: str | None = None
```

### AgentResult

> **已存在，应形式化而非新建**：运行时已有 `_build_agent_result`（`engine.py:1097+`，返回 summary/findings/files_read/files_modified/tokens/result_path 的 dict）+ `_render_agent_result_envelope` 的 bounded envelope，且**前台 / skill-fork / 后台三条 spawn 路径都已走它**。本节是"把现有 dict 形式化为 dataclass 并补 `branch_id`/`events_path`/`status`/`artifacts`"，不是从零设计。

```python
@dataclass
class AgentResult:
    agent_id: str
    branch_id: str
    status: Literal["completed", "failed", "cancelled", "timed_out"]  # 复用现有 SUBAGENT_STATUSES（已是超集）
    summary: str
    files_read: list[str]        # 不可回归：宿主派生、"绝不信任模型自述"（engine.py:220-224）
    files_modified: list[str]    # 同上；初版 dataclass 漏了这两个字段会丢掉反伪造保证
    result_path: str | None
    messages_path: str | None
    events_path: str | None
    tokens: dict
    artifacts: list[ArtifactRef]
```

保留 `_render_agent_result_envelope` 已调优的行为（~4KB passthrough、10 项上限 + 溢出计数、terminal/timeout 部分结果处理），不要退回到"Summary + Full result:"三行草图——那会回归简洁结果直传与文件/发现可见性。

### ApprovalRequest

> approval 须能表达**嵌套**：前台子 agent 跑在父 turn 被 await 的 tool call 里、深若干帧、共享同一 `confirm_fn`（引用传递）。扁平的 `turn_id` 表达不了"父+子同时挂起"。因此带上 `agent_id` + `depth`，复用现有 `_confirm_dedupe_key`/`_decorate_confirm_message` 的身份元组（`artifact_id`/`agent_type`/`source`）。

```python
@dataclass
class ApprovalRequest:
    id: str
    session_id: str
    thread_id: str
    turn_id: str
    agent_id: str                # 发起方身份（嵌套时区分父/子），复用 artifact_id
    depth: int                   # 0=主 agent；>0=前台子 agent 帧深度
    source: Literal["tool", "hook", "subagent", "sandbox"]
    action: str
    message: str
    data: dict
```

语义（详见 Capabilities / 取消语义段）：一个 approval **只挂起发起方那一帧**，不是整个 turn；同一 turn 可合法地在不同 depth 有多个未决 approval；后台 / 非交互 agent 的 approval 必须由 runtime **立即**解析为 deny（"非交互"），绝不投影成等客户端回应的悬空请求。

## 事件类型建议

> 这些是**目标词表**，不是现状清单。代码已 emit 的 13 个名字与它们的对应关系见上文「现有事件源对账 → 事件词表映射」表；落地时按该表把现有名提升为 durable type，不要并行造一套新名。

第一批应覆盖现有能力：

```text
session_started
session_resumed
session_archived

branch_created
branch_switched
branch_summary_created

turn_started
turn_completed
turn_failed
turn_cancelled

message_appended
assistant_delta

tool_call_started
tool_call_completed
tool_call_failed

permission_requested
permission_resolved

task_created
task_updated
task_completed
task_failed
task_cancelled

subagent_started
subagent_completed
subagent_failed
subagent_cancelled

artifact_written
snapshot_written

compaction_started
compaction_completed
compaction_failed

hook_started
hook_completed
hook_blocked
```

第二批再开放给 extension/hook：

```text
before_context_build
after_context_build
before_tool_use
after_tool_use
before_compaction
after_compaction
before_branch_fork
after_branch_fork
subagent_start
subagent_stop
session_stop
```

## SessionEventStore 设计

`SessionEventStore` 是 Pi 化内核的第一块地基。

职责：

```text
append(event) -> event
read(session_id) -> iterator[SessionEvent]
read_branch(session_id, branch_id, leaf_id=None) -> list[SessionEvent]
get_leaf(session_id, branch_id) -> event_id | None
set_leaf(session_id, branch_id, event_id) -> None
fork_branch(session_id, from_event_id, new_branch_id) -> None
write_snapshot(session_id, snapshot) -> None
read_snapshot(session_id) -> dict | None
```

推荐目录：

```text
~/.nanocode/sessions/<session_id>/
  events.jsonl
  branches.json
  state.json
  main/messages.json
  agents/<agent_id>/
    messages.json
    result.md
  tasks/<task_id>/
    meta.json
    result.md
    stdout.log
    stderr.log
  artifacts/
```

`events.jsonl` 是事实源。`branches.json` 只记录 branch leaf cache：

```json
{
  "main": {
    "leaf_id": "evt_000123",
    "label": "main"
  },
  "agent_001": {
    "leaf_id": "evt_000140",
    "parent_branch_id": "main",
    "forked_from": "evt_000122"
  }
}
```

### 耐久性与恢复语义（事实源级，必须设计而非默认）

一旦 `events.jsonl` 是事实源，"malformed 行不致整个 session 读失败"远远不够。现有 `JsonlSink`/`store.py`/`v2.py` 的统一风格是**出错静默吞掉 / 自禁用**——这对可观测性（trace）正确，对事实源**不可接受**。新 event store 必须显式背离这一家风：

1. **冲突权威**：`events.jsonl` 对 `branches.json` 权威。`get_leaf()` 必须能靠扫描 `events.jsonl` 重建 leaf；`branches.json` 仅性能提示。启动时若它指向无法解析/不存在的 `leaf_id`，从日志重算并重写。（Risk 2 只覆盖 events-vs-snapshot，未覆盖 cache-vs-log，本节补上。）
2. **torn-tail**：`JsonlSink` 逐行 flush 但**不 fsync**，崩溃可留半行。读时若末行不完整/不可解析，截断到最后一条完好事件并 emit 一个恢复标记事件（如 `recovered_truncated_tail`），不静默丢弃。至少在 `turn_completed` 上 fsync，保证完成的 turn 跨崩溃可达。
3. **禁止静默跳过文件中段的坏行**：中段（非尾部）的 malformed 行意味着损坏；越过它可能让某 tool_result 变孤儿、破坏配对不变量。策略：**拒绝加载配对已破的历史并报错**，而不是加载一个微妙错误的历史。
4. **写失败必须浮现**，不得照搬 `trace/sinks.py:34-35` 的"首次出错即自禁用、后续事件无声丢弃"。

### Session 归属与并发（P5/P6/P7 前置）

长驻 App Server + 仍保留的 in-process 模式 + SDK 可同时持有一个 `session_id`；而当前持久化是**无锁 last-writer-wins**（`_auto_save`/`_persist_state` 直接覆写 `state.json`/`messages.json`，无跨进程锁）。在任何外部入口上线前定下：

1. **单写者**：每个 `session_id` 经 advisory lockfile（如 `flock` on `<session>/owner.lock`）或 server 持租约保证恰一写者；读者（replay/`/tree`/CLI tail/SDK stream）只读打开、容忍文件增长。
2. **resume 占用策略**：`thread_resume` 命中被他人占用的 session 时，行为显式三选一——拒绝（"session busy"）、只读 attach（观察实时事件）、或接管（撤销前租约）。协议与 SDK 必须内建，不能事后补。
3. **写原子性**：快照（`state.json`/`messages.json`）当前是非原子 `write_text`（`session/v2.py:24`），崩在写中即损坏——改 write-temp-then-`os.replace`。`events.jsonl` append 走单一 owner 写者，不依赖 `O_APPEND` 原子性。
4. **fork 验收要诚实**：两进程往同一 `events.jsonl` append 无法靠文件模式满足"互不覆盖"——要么每 branch 独立 append target，要么全部经单一 event-store 写者路由。

`events.jsonl` 是事实源。`branches.json` 只记录 branch leaf cache。

## SessionContextBuilder 设计

`SessionContextBuilder` 负责从 event tree 生成 provider messages。

职责：

```text
build(session_id, branch_id, leaf_id=None, budget=None) -> ContextBuildResult
walk leaf -> root
include message_appended events
apply compaction summaries
apply branch summaries
include selected memory/subagent summaries
preserve tool_use/tool_result pairing
return provider-neutral messages + diagnostics
```

这样后续可以稳定支持：

- `/tree`
- `/fork`
- `agent(context="fork")`
- branch summary
- structured compaction
- replay/debug
- SDK reconnect
- IDE timeline

初期可以只支持 main branch 的 `message_appended` 重建。不要一开始就做完整 compact/fork。

### 关键修订：「events 是事实源、snapshot 是 cache」是分阶段命题

初版把它当成无条件前提，但在当前代码下**不成立**：模型循环每轮都**原地销毁/改写**消息列表——

- `_compact_conversation` 整体清空并替换 `_anthropic_messages`/`_openai_messages`（summary 化）；
- `_run_compression_pipeline`（在 `while True` 循环内、每次 API 调用前跑）把 tool_result 内容就地改成 `SNIP_PLACEHOLDER` / `[Old result cleared]` / 30KB 落盘预览（`compaction.py`，`SNIP_THRESHOLD=0.60`，microcompact 频繁触发）。

因此 `messages.json` 是模型**真正看到**的事后状态；仅靠 append-only 的 `message_appended` walk leaf→root **重建不出同一上下文**。结论：

1. **迁移期 resume 仍以 snapshot 为准**，`events.jsonl` 先只做可观测；只有当 compaction/snip/budget/microcompact 全部事件化且可确定性重放后，ContextBuilder 才成为权威 resume/fork 路径。Risk 2 与持久化兼容策略据此改写。
2. **取代/墓碑（supersession）契约**（P5 fork/context-builder 阶段定，MVP 不需要）——直接对标 Pi 的 `CompactionEntry`（[session-format](https://pi.dev/docs/latest/session-format)：compaction **追加**不改写，重建时先放 summary、再放 `firstKeptEntryId` 之后的消息）：
   - `compaction_completed` 是 append-only 事件，携带 `{superseded_event_id_range, summary_text, affected_tool_use_ids}`（≈ Pi 的 `{summary, firstKeptEntryId, tokensBefore}`）；原 `message_appended` 事件保持不可变。
   - 三个裁剪 tier 不各自 emit 重事件：用单条按 `tool_use_id` 滚动的 `tool_result_truncated`，或在 build 时按一个存储的 utilization marker 确定性重算——二选一并写明（当前两者皆无）。
   - ContextBuilder 把 compaction 事件当作 supersession marker：跳过被取代 id、替换为 summary、确定性重应用 tier 截断。
3. **配对重建校验**：build() 后断言每个 assistant tool_use/tool_call id 恰有一个匹配 tool_result、无孤儿；畸形重建**大声拒绝**而非交给 provider。把 deny/confirm-deny 的合成 tool 结果、并行 gather 批次列为 `tests/session/test_context_builder.py` 的具名用例。（"顺序"不是正确性不变量——两家 provider 都按 id 配对；配对基数与 pair-boundary 完整性才是。）
4. **硬验收**（P3/P5 门槛）：跑一个触发 budget+snip+microcompact+full-compaction 的会话，断言"从事件重建的消息数组 == 快照里模型实际看到的数组"逐条相等。这是证明"cache 真的可派生"的那个测试。

> 性能注记：ContextBuilder **每个用户 turn 调一次**，不是每个 LLM round-trip 的热路径；活跃循环内仍持有内存消息列表。完整 leaf→root walk 只发生在 fork/resume。P3 验收加一条 per-turn 开销预算，避免回归现有内存 append 路径。

## AgentSession 设计

`AgentSession` 是 Pi 化内部会话对象。

职责：

```text
append_user_message()
run_turn()
append_assistant_message()
append_tool_event()
spawn_subagent()
compact()
fork()
subscribe()
```

它不负责 JSON-RPC，也不负责 CLI 渲染。

> **前置真相：`AgentSession` / `RuntimeThread` / `AgentCore` 目前是同一个对象（2037 行 `Agent`）的三个名字。** 它同时拥有 `_*_messages` 列表、跑模型循环、在循环内 compaction、驱动子 agent、并耦合 UI（`_emit_text` 按 `is_sub_agent` 分叉、`engine` 直接 import `rich`/`ui`）。在命名三层之前，必须先做那个让三层有意义的**解耦 refactor**（见路线图 P-1）：
> 1. 把 compaction/snip/budget pipeline **移出 `while True` 循环**，让循环消费一个它不改写的 context；
> 2. 给 `_*_messages` 指定**唯一 owner**（归 `AgentSession`），循环只读 built context、返回待 append 的事件；
> 3. 父不再按引用写 `sub_agent._*_messages`（`engine.py:1880-1882`），改走 session/branch API；
> 4. `_emit_text`/spinner/print 走**注入的 event sink**，core 不再 import `..ui`/`rich`。
>
> 验收即"用 fake event store + 无 UI 构造 core 跑一个 turn"。在该 seam 存在前，`AgentSession`/`RuntimeThread` 只是同一 `Agent` 的 wrapper，只增 indirection、不产生隔离。

内部调用关系：

```text
RuntimeThread.run(prompt)
  -> AgentSession.run_turn(prompt)
       -> SessionEventStore.append(user message)
       -> SessionContextBuilder.build(...)
       -> AgentCore.run(...)
       -> Capabilities.execute_tool(...)
       -> SessionEventStore.append(tool/subagent/task/artifact events)
       -> SessionEventStore.append(final assistant message)
       -> TurnResult
  -> RuntimeThread projects events to client
```

## AgentRuntime / RuntimeThread 设计

`AgentRuntime` 是 Codex 化 facade。

职责：

```text
thread_start(config) -> RuntimeThread
thread_resume(session_id) -> RuntimeThread
thread_fork(thread_id, from_event_id) -> RuntimeThread
thread_list()
thread_archive(thread_id)
load_config()
setup_tool_registry()
setup_mcp()
setup_sandbox_defaults()
```

`RuntimeThread` 是外部 API 面向的会话句柄：

```text
run(prompt, options) -> TurnResult
cancel(turn_id)
events() -> async iterator[RuntimeNotification]
approve(approval_id, decision)
tasks()
agents()
fork()
```

命名建议：

- 内部状态对象叫 `AgentSession`，强调 Pi-like session tree。
- 外部协议对象叫 `RuntimeThread`，贴近 Codex thread 语义。

> **MCP 生命周期不是干净的 `setup_mcp()`**：当前 MCP 是 lazy（首次 chat 时连）、**仅主 agent**、就地改 `self.tools`、失败静默 `print`（`engine.py:388-398`），子 agent 从不拿 MCP 工具。`AgentRuntime` 必须显式决定：(1) MCP 连接应几乎肯定上移到 **runtime 级**（每进程/每 config 连一次、共享 tool defs），而非 per-thread lazy（否则每 thread 重连会倍增 server 进程）；(2) `context=fork/summary/isolated` 子 agent **是否**继承 MCP 工具——既然子 agent 升为一等 thread，现有 `not self.is_sub_agent` 的一刀切排除必须改成由 `mcp_allow` 表达的**显式策略**，而非 init 位置的偶然；(3) 失败 `print` 改为 tracer 事件，让 MCP init 失败可审计。

## 取消 / 中断语义（不可回归契约）

`RuntimeThread.cancel(turn_id)` / `turn.cancel` / `task.stop` 不是新能力——取消已存在且有硬挣来的细节，协议化时**只允许保留、不允许悄悄回归**：

1. **`Agent.chat()` 故意吞 `CancelledError` 进 `_aborted`**（`engine.py:404-408`）。这正是子 agent **不能裸 await `chat`** 的原因（`engine.py:992-996` 注释：裸 await 会把真实取消误判成成功）。故 `RuntimeThread.cancel` / 任何 turn wrapper **必须查 `_aborted`、绝不能把协程正常返回当成功**；复用 `_await_subagent_run`（`engine.py:1718-1758`）这个既有原语，别重新推导。
2. **区分 turn-cancel 与 thread/task-stop**：今天 `abort()` 只取消 `_current_task`，`_background_tasks` 仍在跑。必须写明父 turn 取消时对在飞的后台子 agent 的行为（cancel-and-await 还是 detach）——当前是不对称的。
3. **保留两条不变量**：`kind=='timeout'` 但无 timeout 时折叠为 `cancelled`；以及"首个 await 前取消"竞态（`engine.py:1270-1271`）。`turn_cancelled` 事件 emission 不得丢这两条。
4. **`_aborted` → `TurnResult.status='cancelled'`** 的映射要显式。
5. 取消一个 turn 必须**同时解决该 turn 在所有 depth 的未决 approval**（deny/cancel），并唤醒任何在等 `approval.respond` 的客户端。单进程 REPL 现靠 per-agent `_current_task` 取消 + `_async_read_line` 的 SIGINT handler 还原来编排这件事；App Server 下这套编排消失，协议必须接管。

## App Server 协议

第一版只做 stdio JSONL，不做 HTTP。**App Server / SDK / 多 transport 属 aspirational 层（见路线图与建议优先级）：在出现具体的第二个 client 之前不落地。** 且任何外部入口都必须复用与 CLI 同一个 fail-closed 能力闸，不得开第二个绕过它的入口。

### 初始化

```json
{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"clientInfo":{"name":"nanocode-cli","version":"1.0.0"}}}
```

响应：

```json
{"jsonrpc":"2.0","id":0,"result":{"serverInfo":{"name":"nanocode-app-server","version":"0.1.0"},"capabilities":{"threads":true,"approvals":true,"events":true}}}
```

### Thread lifecycle

```json
{"jsonrpc":"2.0","id":1,"method":"thread.start","params":{"cwd":"/repo","model":"...","permission_mode":"default"}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"thread.resume","params":{"session_id":"sess_abcd"}}
```

```json
{"jsonrpc":"2.0","id":3,"method":"thread.fork","params":{"thread_id":"thr_1","from_event_id":"evt_123","branch_id":"experiment"}}
```

### Turn lifecycle

```json
{"jsonrpc":"2.0","id":4,"method":"turn.run","params":{"thread_id":"thr_1","prompt":"修复 CI 失败"}}
```

server notifications：

```json
{"jsonrpc":"2.0","method":"event","params":{"thread_id":"thr_1","turn_id":"turn_1","type":"turn_started","data":{}}}
```

```json
{"jsonrpc":"2.0","method":"event","params":{"thread_id":"thr_1","turn_id":"turn_1","type":"tool_call_started","data":{"tool":"grep_search","input":{"pattern":"ERROR"}}}}
```

### Approval

server request（带发起方身份 + 帧深度，支持嵌套子 agent）：

```json
{"jsonrpc":"2.0","id":"apr_001","method":"approval.request","params":{"thread_id":"thr_1","turn_id":"turn_1","agent_id":"agent-002","depth":1,"source":"subagent","message":"Allow shell command?","data":{"command":"pytest -q"}}}
```

client response：

```json
{"jsonrpc":"2.0","id":"apr_001","result":{"approved":true}}
```

> 一个 approval 只挂起 `agent_id` 那一帧，不是整个 turn；同一 turn 可有多个不同 depth 的未决 approval。后台/非交互 agent 的请求由 runtime 立即 deny，绝不下发给客户端等待。

### Task/subagent

```text
task.list
task.output
task.stop
agent.list
agent.show
agent.send
agent.stop
```

第一版不需要暴露所有 slash command。先覆盖 runtime 基础能力。

## CLI 改造方向

最终 `src/nanocode/entrypoints/cli.py` 应只承担：

```text
argument parsing
REPL input
slash command client
event rendering
approval UI
local app-server launch or in-process runtime bootstrap
```

不再承担：

```text
直接创建 Agent 并注入所有 runtime 依赖
直接恢复 session internals
直接读写 task/subagent state
直接实现 approval business logic
直接渲染 core 内部 print
```

迁移策略：

1. 第一阶段 CLI 仍然 in-process 调 `AgentRuntime`。
2. 第二阶段 CLI 可选 `nanocode serve --stdio`。
3. 第三阶段 CLI 默认像 Codex TUI 一样启动 app-server child process，通过 JSON-RPC 渲染事件。

## Subagent 改造方向

当前 nanocode subagent 已支持自定义 manifest、foreground/background、resume、timeout。下一步应从“task-like agent”改成“session branch/thread-like agent”。

### 新增 agent 工具字段

```json
{
  "context": "isolated | summary | fork",
  "isolation": "none | sandbox | worktree",
  "return_mode": "summary | artifact | full",
  "max_turns": 10,
  "timeout_ms": 600000
}
```

语义：

- `context=isolated`：**已是当前默认行为**（`engine.py:895-940`：不继承父历史、`artifact_id`-scoped messages/wire、后台用全新 `confirmed_paths`）。只记录 parent linkage。本项不是新工作。
- `context=summary`：注入父分支摘要（待建）。
- `context=fork`：从父当前 event leaf 创建真实 branch（待建）。**依赖事件级 `id`/`parent_id` 树链接**——现有 tracer 只有 `session_id`/`parent_session_id`/`seq`，没有事件级父，故 fork **不能先于事件树链接落地**（见现有事件源对账）。
- `return_mode=summary`：父上下文只收到摘要和 artifact path。
- `return_mode=full`：保留兼容，但应限制长度。

### SubAgentRecord 扩展（只增不减）

> **现有 `SubAgentRecord`（`tasks/models.py:38`）已有 `id/type/description/status/model/provider/created_at/updated_at/message_path/last_result_path/task_id`，且 `from_dict` 在 resume 时被读。** 初版替换式 dataclass 静默删掉了 `model/provider/created_at/updated_at/description` 并把 `last_result_path` 改名 `result_path`——会**破坏老会话 resume**（resume 期 model guard `engine.py:1853` 依赖 `rec.model`；`/agents` 详情依赖这些字段；5 处写点依赖 `last_result_path`）。必须严格 additive：

```python
@dataclass
class SubAgentRecord:
    # —— 现有字段，全部保留 ——
    id: str
    type: str = "coder"
    description: str = ""
    status: str = "idle"          # 复用现有 SUBAGENT_STATUSES（已含 timed_out/lost/cancelled）
    model: str | None = None
    provider: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    message_path: str | None = None
    last_result_path: str | None = None   # 勿改名为 result_path
    task_id: str | None = None
    # —— 本次新增 ——
    branch_id: str | None = None
    parent_session_id: str | None = None
    parent_event_id: str | None = None
    context_mode: str = "isolated"
    result_event_id: str | None = None
```

`from_dict` 必须能 round-trip 老会话（已有的 `{k: v for k,v in d.items() if k in keys}` 过滤即可）。

### 输出策略

> 见「核心数据模型 → AgentResult」：三条 spawn 路径**已统一**走 `_build_agent_result`，故"统一前台/后台返回"对现有字段已达成；新工作仅是补 `branch_id`/`events_path`/`status`/`artifacts` 并保留 `files_read`/`files_modified` 与 bounded envelope。

父上下文默认只接收摘要 + artifact path（保留现有 `_render_agent_result_envelope` 的调优行为，避免 context pollution）。

## Capabilities / Permission 改造方向

Codex 的关键安全经验是：sandbox 是技术边界，approval 是越界审批。nanocode 应把 permissions 从 tool 层散点逻辑提升为 capability 层。

> **现状校准**：tool 限制**已经 fail-closed 强制**——`_execute_tool_call` 在任何真实派发前调 `_tool_blocked_by_allowlist`（`engine.py:687`，覆盖前台 + 后台 run_shell），P4 已落地、有 `tests/agent/test_subagent_callgate.py` 兜底。但 enforcement 目前分两条路径：per-tool `check_permission`（backends）与 subagent `_tool_blocked_by_allowlist`（engine）。`PermissionEngine` 的价值因此是**把两条路径合并成单一可测咽喉点**（不是"首次引入 enforcement"），让未来的 App Server/SDK 入口继承同一决策点而非各自绕路。这是全计划最高价值的一项，应升为早期编号阶段（见路线图 P0.5），而非仅"改造方向"旁白。仍真正 advisory/未闭合的边是：原生 sandbox 的残余 confinement 边界、project-local agent/hook 的 trust gate——这些才是 PermissionEngine 应吸收的 net-new enforcement。

建议抽象：

```python
@dataclass
class CapabilityPolicy:
    tools_allow: set[str] | None
    tools_deny: set[str]
    fs_read_roots: list[str]
    fs_write_roots: list[str]
    shell_sandbox: str
    network: Literal["none", "restricted", "enabled"]
    allow_escalation: bool
    mcp_allow: set[str] | None
    subagents_allow: bool
    subagents_max_depth: int
    hooks_allow: bool
```

统一决策：

```text
PermissionEngine.check(action) -> allow | deny | request_approval
```

适用于：

- built-in tools
- MCP tools
- shell/sandbox shell
- hooks
- subagent spawn
- file edits
- project-local agents
- future extensions

后台和非交互模式：

- 无法发起新 approval 时 fail-closed 或 auto-deny（已有 `_auto_deny_confirm`，`engine.py:111`）。
- hook 不应成为唯一安全边界。
- project-local agent/hook/extension 必须有 trust gate。

外部入口的两个新边界（gating P6/P7）：

- **transport 鉴权**：unix socket / websocket 没有鉴权边界——任何本地进程连上即继承启动用户的全部权限。明确 v1 仅 stdio（设计如此、无鉴权面），非 stdio transport 须先有 per-connection 握手/鉴权机制。
- **跨项目信任**：`trust.py` 按启动时 cwd（git toplevel key）授信，但长驻 server 可 `thread.start(cwd=…)` 进入另一个未授信项目（`turn.run` 的 params 带 cwd）。trust 必须**按 `thread.start` 的 cwd 重新校验**，而非仅进程启动时一次。

## Hooks / Extensions 改造方向

nanocode 不应早期照搬 Pi 的任意代码 extension。建议分阶段：

### 阶段 1：内部 lifecycle events

所有核心能力先写入 EventBus：

```text
SessionStart
SessionResume
BeforeContextBuild
AfterContextBuild
TurnStart
MessageAppend
PreToolUse
PostToolUse
PermissionRequest
CompactionStart
CompactionEnd
BranchFork
SubagentStart
SubagentStop
Stop
```

### 阶段 2：声明式 hooks

保留当前 skill hooks 思路，但接入统一事件和 permission：

```yaml
hooks:
  pre-tool-use:
    - matcher: run_shell
      command: python .nanocode/hooks/check_shell.py
      timeout: 30
      trust: project
```

### 阶段 3：声明式 extension

一个 extension 先只能声明：

```text
agents
skills
MCP servers
slash commands
hooks
tool wrappers
```

### 阶段 4：代码式 extension

后置。必须要求：

- trust hash
- source scope
- sandbox/capability
- disable/allowlist UI
- managed policy override

## 持久化兼容策略

不能一次性废弃现有 session。

建议：

1. 保留 `session/v2.py` 现有 API。
2. 新增 `session/event_store.py`（按"现有事件源对账"，它 promote 现有 tracer/wire，而非并列第三 lane）。
3. **event append 必须无条件、走每-turn 路径**——不要绑定到 `write_main_messages`/`write_agent_messages`。原因：v2 快照写入是**有条件**的（`_auto_save` 仅在 `is_v2_session` 或有 fork 子 agent 时才调 `_persist_state`→`write_main_messages`，`engine.py:530-532`；`write_agent_messages` 只在子 agent 路径触发）——普通单 agent 会话两者都不触发。而 event 应挂在 P1 已用的 `Agent.chat`/`run_once`/`_execute_tool_call`，即现有 tracer 始终 emit 的那条 always-on 路径（`engine.py:401/410/428`，wire sink 无条件 wired 于 `283-287`）。
4. `load_session` **迁移期仍以 snapshot 为权威 resume 路径**，event store 仅增强/可观测；只有 compaction/snip 事件化且重建通过 byte-equal 验收后，才切到从事件 resume（见 ContextBuilder 段）。
5. 新 session 默认创建 `events.jsonl`。
6. 老 session resume 时可 lazy migration：第一次写入 event store 时记录 `legacy_imported` event。
7. **明确 wire.jsonl 去向**：本节须与"现有事件源对账"的取舍一致——`events.jsonl` 取代或聚合 wire，不得让 P1 悄悄变成第四条写路径。

兼容事件：

```json
{
  "type": "legacy_snapshot_imported",
  "data": {
    "source": "main/messages.json",
    "message_count": 42
  }
}
```

> **schema 演进**：`events.jsonl` 作为事实源，老会话须被新代码永久（或至迁移点前）可重放。`SessionEvent` 已带 `v`（沿用 tracer 的 `SCHEMA_VERSION=1`，勿重置）。补一个 upcaster：读端**容忍未知 `type` 与未知 `data` key**（skip-and-warn，绝不 crash 会话），由 upcaster 把旧事件形状映射到当前再交给 ContextBuilder。仓库已有范式可借：`memory/engines/simplemem/migrations.py` 的 schema marker + fail-loud migration guard。可选：resume 的老会话惰性重快照为当前 schema、归档 pre-upcast 原始事件，使 upcaster 面有界。

## 迁移路线图

> **排期总则（修订）**：先做 abandon-safe 的内部清理与事件统一，把结构性 live-path churn（AgentSession/AgentRuntime facade）推迟到真有第二个 caller。带 ⚠ 的阶段会 mutate live path，半途放弃会让产品变差——它们必须"无其它迁移在飞"时才动。带 ☁ 的阶段是 aspirational（无消费者前不落地）。

### P-1：解耦 refactor（facade 的前置，abandon-safe）

这是让 `AgentSession`/`RuntimeThread`/`AgentCore` 三层"有意义"的唯一前置，且是纯内部清理、放弃也不留半成品：

- 把 compaction/snip/budget pipeline 移出 `while True` 循环，循环消费不被它改写的 context。
- `_*_messages` 指定唯一 owner；父不再按引用写 `sub_agent._*_messages`（`engine.py:1880-1882`）。
- `_emit_text`/spinner/print 走注入 sink，core 停止 import `..ui`/`rich`。

验收：用 fake event store + 无 UI 构造 core 跑一个 turn；现有用例不退化。

### P0：事件模型和存储（promote 现有 tracer，非 greenfield）

新增/改造：

- `src/nanocode/events/models.py`（`SessionEvent` = tracer 事件 dict 的超集，复用 `v`/`seq`/`ts`/`session_id`）
- `src/nanocode/session/event_store.py`（promote per-agent `wire.jsonl` 为 session 级 `events.jsonl` 的写入点）
- 给 `Tracer.emit` 的事件 dict 补 `id`/`branch_id`/`turn_id`/`agent_id`/`parent_id`
- 对应测试

目标：

- 定义 `SessionEvent`（带 `v`）。
- 支持 append/read，单一 owner 写者。
- 支持 `branches.json` leaf cache，且 events 对它权威。
- 不改现有行为。

验收：

- `pytest tests/session/test_event_store.py -q`
- append 1000 条事件后能稳定读取。
- malformed **尾**行截断到最后完好事件并 emit 恢复标记；malformed **中段**行拒绝加载并报错（不静默跳过）。
- `branches.json` 指向坏/不存在 `leaf_id` 时，扫描日志重算 leaf。
- 写失败浮现（不照搬 `sinks.py` 自禁用）。

### P0.5：PermissionEngine 合并（安全咽喉点，App Server/SDK 的硬前置）

把现有两条 enforcement 路径（backends 的 `check_permission` + engine 的 `_tool_blocked_by_allowlist`）合并为单一 `PermissionEngine.check(action) -> allow|deny|request_approval`。

验收（不变量测试）：扩展 `test_subagent_callgate.py`，断言每条真实工具派发路径（前台、后台 run_shell、hook shell、MCP、未来 server-routed turn）都过同一闸；新增一条会让"绕过闸的新派发路径"失败的测试。**P6/P7（任何外部入口）gating 在本阶段完成且 enforce 之上。**

### P1：现有 runtime 单写 fan-out 事件（非"双写"）

> 修订：不是在每个 `tracer.emit` 旁再加第二个独立 emitter（那会带来两套词表、两条 finalize/close、两套 seq——正是 `engine.py:426-432` 刚 hardening 过的句柄泄漏面翻倍）。而是让现有 emit 调用点 feed 同一个 bus，`events.jsonl` 作为"再挂一个 sink"，继承已 hardening 的 close 路径。

接入点（已是现有 tracer emit 的点）：

- `Agent.chat` / `Agent.run_once` / `_execute_tool_call`
- `_spawn_background_subagent` / `_run_background_subagent`
- `task_manager.update_task`
- session save/restore

目标：

- 主消息、工具调用、任务、subagent、artifact 都进 `events.jsonl`（按词表映射用现有名）。
- CLI 行为不变。
- **明确 wire.jsonl 去向**（取代/聚合，见对账），不留第三 lane。

验收：

- 运行一次普通任务后 `events.jsonl` 有 turn/message/tool events。
- 运行 background subagent 后有 task/subagent/artifact events。
- **parity**：若暂时保留 wire.jsonl，则 `events.jsonl` 的 tool_call/tool_result/turn 边界与 wire 1:1 同序，且在 error/timeout/cancel 路径双双 finalize（`engine.py:426-432` 的路径）。

### P2：EventSink 替代核心 print（低风险、近 additive）

> 代码已在每个 print 旁 emit 对应事件，故 P2 是"把现有 print 路由到事件已喂的同一 sink"。dual-render 是**过渡态**：一个明确的 flip commit 在 sink 渲染一致后移除直接 print——不留长期 dual 窗口。半途放弃 CLI 仍完全可用。

目标：

- `Agent` 接收 event sink。
- backend 不直接调用 terminal UI（过渡期可短暂 event + print 并存，由单个 flip commit 收口）。
- CLI 通过 event sink 渲染 tool call/result。

验收：

- 单测可用 memory sink 捕获 tool events。
- CLI 输出保持基本一致。

### ⚠ P3：抽 AgentSession（mutate live path，需"无其它迁移在飞"）

> 这是 abandon-unsafe 的高 churn 阶段：插入 `AgentSession.run_turn` 会产生两条 turn 入口（CLI→Agent 与 CLI→AgentSession→Agent），resume 逻辑分叉于 v2 snapshot 与 context_builder。**前置 P-1 必须已完成**，且无其它迁移在飞时才动。

新增：

- `src/nanocode/session/context_builder.py`
- `src/nanocode/runtime/session.py` 或 `src/nanocode/session/agent_session.py`

目标：

- `AgentSession.run_turn()` 管 append user event、构造 context、调用 Agent、append result events。
- 初期 context builder 仍从 existing messages snapshot 构建（resume 仍以 snapshot 为权威），但接口要稳定。

验收：

- CLI 通过 `AgentSession` 跑一轮。
- 现有 resume 测试不退化。
- per-turn builder 开销在声明预算内（vs 现有内存 append 路径）。

### ☁ P4：抽 AgentRuntime / RuntimeThread（facade，无第二 caller 前可缓）

新增：

- `src/nanocode/runtime/runtime.py`
- `src/nanocode/runtime/thread.py`
- `src/nanocode/runtime/results.py`
- `src/nanocode/runtime/approvals.py`

目标：

- CLI 创建 `AgentRuntime`，再 `thread_start` 或 `thread_resume`。
- `RuntimeThread.run()` 返回 `TurnResult`。
- approval 从 `confirm_fn` 包装成 `ApprovalManager`。

验收：

- CLI 仍可交互使用。
- 非交互测试可注入 approval handler。

### P5：Session tree / fork

> 真正从事件重建上下文的阶段：在此之前必须落地 compaction/snip 的 supersession 事件契约 + 配对重建校验（见 ContextBuilder 段），否则 fork branch 的上下文无法忠实重建。

目标：

- 支持 branch leaf。
- 支持 `thread.fork(from_event_id)`。
- 支持 `/tree` 最小文本输出。
- 支持 `agent(context="fork")` 使用真实 branch。

验收：

- 从同一 event fork 两个 branch，各自 append 事件互不覆盖（经单一 owner 写者或 per-branch target 保证，非靠 append 模式）。
- context builder 能从 branch leaf 重建消息。
- rebuild-after-compaction == 模型实际看到的消息数组（byte/逐条相等）。

### ☁ P6：App Server stdio（aspirational，gating 在 P0.5 enforce + 第二个 client 存在）

新增：

- `src/nanocode/protocol/*`
- `src/nanocode/server/app_server.py`
- `src/nanocode/server/transports/stdio.py`
- CLI flag: `nanocode serve --stdio`

第一版方法：

```text
initialize
thread.start
thread.resume
thread.list
thread.fork
turn.run
turn.cancel
approval.respond
task.list
task.output
task.stop
agent.list
agent.show
```

验收：

- 一个 test client 可以启动 server、start thread、run turn、收到 events。
- approval request 能暂停 turn 并等待 client response。

### ☁ P7：CLI 切到 runtime client（aspirational）

目标：

- CLI 默认仍可 in-process。
- 增加 `--remote stdio|unix://...` 或内部 app-server child process。
- slash commands 通过 runtime/thread API 操作。

验收：

- CLI 行为和当前用户体验基本一致。
- 可以连接已运行的 app-server。

### ☁ P8：Python SDK（aspirational）

目标：

```python
from nanocode.sdk import Nanocode, Sandbox

with Nanocode() as nc:
    thread = nc.thread_start(cwd="/repo", sandbox=Sandbox.workspace_write)
    result = thread.run("修复测试")
    print(result.final_response)
```

验收：

- SDK 不 import `Agent` 内部。
- SDK 可以 resume thread。
- SDK 可以 stream events。

## 测试计划

新增测试目录建议：

```text
tests/events/
  test_event_models.py
  test_event_bus.py

tests/session/
  test_event_store.py
  test_branch_store.py
  test_context_builder.py
  test_legacy_migration.py

tests/runtime/
  test_agent_runtime.py
  test_runtime_thread.py
  test_approvals.py
  test_turn_result.py

tests/protocol/
  test_jsonrpc_dispatcher.py
  test_stdio_transport.py

tests/subagents/
  test_subagent_context_modes.py
  test_subagent_agent_result.py
```

> `tests/protocol/` 跟随 ☁ P6 一并 gating——若 App Server 在战略评估后被砍，它即死重。

关键场景：

- events append-only，不覆盖用户/历史数据。
- fork branch 互不污染。
- tool call/result pairing 不被 context builder 拆坏（含 deny/confirm-deny 合成结果、并行 gather 批次）。
- approval request 在 CLI、noninteractive、background 三种场景行为明确。
- subagent 前台/后台都返回 `AgentResult`。
- project-local agents/hooks 未 trust 时不能静默执行高风险能力。
- 老 session 能 resume。

补三条当前代码已证脆弱、却被初版漏掉的高危场景（按风险排序）：

- **（最高）取消传播**：取消父 turn 且有在飞后台子 agent 时，断言 `TurnResult.status=='cancelled'` 且子 agent 落到终态 `subagent_cancelled`——取消**不得**被 `chat()` 吞成 `completed`（对应 `engine.py:992/1284/1724` 已注释的 footgun）；并断言两个兄弟前台子 agent 的 approval **不**被互相 dedupe（`_confirm_dedupe_key` 不变量）。
- **（高）单 session 并发**：两个写者写同一 `events.jsonl`——要么断言被序列化/拒绝（单写者不变量），要么显式声明并测试该不变量。
- **（高）崩溃中段 resume + schema 演进**：无 `turn_completed` 即崩溃后 resume，断言重建用最后一条已提交事件、且容忍截断尾行；以及 vN 写、vN+1 读的 round-trip（扩展 `test_legacy_migration.py`，今天只覆盖 snapshot→event）。
- **rebuild-after-compaction**：触发 budget+snip+microcompact+full-compaction 后，从事件重建 == 快照逐条相等。

## 风险和取舍

### 风险 1：一次性重构过大

控制方式：先做 abandon-safe 的 P-1 解耦 + 事件统一（promote tracer），不改变行为。不要第一 PR 就把 CLI 切成 server client。带 ⚠ 的 mutate-live-path 阶段须"无其它迁移在飞"时才动。

### 风险 2：events / snapshot / trace / wire **多事实源**冲突（重写）

初版只谈 events-vs-snapshot，漏了真正的三/四源问题：per-agent `wire.jsonl` + 可选 `traces/*.jsonl` + 拟新增 `events.jsonl` + snapshot。控制方式：(a) 按"现有事件源对账"先 promote tracer/wire 为唯一事实源、`traces/` 降级纯读端，不让三条 durable lane 并存；(b) **迁移期 resume 仍以 snapshot 为权威**，events 仅可观测，直到 compaction/snip 事件化且 rebuild 通过 byte-equal 验收才切换——因为当前 compaction 原地销毁历史，append-only 事件还重建不出模型实际看到的上下文。

### 风险 3：协议过早冻结

控制方式：第一版 JSON-RPC 标记 experimental，先只服务本地 CLI/SDK。方法名和 event type 保持小集合。**且 App Server/SDK 在出现第二个真实 client 前不落地**（见建议优先级），避免给无消费者的协议面过早承诺。

### 风险 4：hook/extension 安全面失控

控制方式：先做内部 lifecycle events，再做声明式 hooks。代码式 extension 后置并要求 trust/capability/sandbox。非 stdio transport 须先有 per-connection 鉴权；trust 按 `thread.start` 的 cwd 重新校验。

### 风险 5：subagent fork 导致上下文膨胀

控制方式：默认仍 `context=isolated`（已是现状）。`context=fork` 需要显式指定，并受 max_depth、max_threads、context budget 控制。

### 风险 6：App Server 使 CLI 调试变复杂

控制方式：保留 in-process runtime mode；server mode 作为后置可选入口。

### 风险 7：事件 schema 演进失控

控制方式：`SessionEvent` 带 `v`（沿用 tracer `SCHEMA_VERSION`）；读端容忍未知 type/key（skip-and-warn）+ upcaster；vN→vN+1 round-trip 测试。借 `memory/engines/simplemem/migrations.py` 的 schema marker / migration guard 范式。

### 风险 8：多进程单 session 写冲突

控制方式：单写者租约 + resume 占用策略 + 快照 `os.replace` 原子写 + `events.jsonl` 经单一 owner。是 P5/P6/P7 的硬前置，不可事后补。

### 风险 9：取消 / approval 嵌套语义回归

控制方式：把"取消/中断语义"与 approval 的 `agent_id`+`depth` 写成不可回归契约 + 测试，**先于**协议冻结（一旦冻结错就是 breaking change）。

### 风险 10：在未 enforce 的能力面上开外部入口

控制方式：P0.5 PermissionEngine 合并为单一 fail-closed 咽喉点，且 gating 所有外部入口；任何 App Server/SDK 路径必须复用同一闸，不得新开绕过它的入口。

## 建议优先级

最高优先级不是 App Server，而是"内部清理 + 事件统一 + 能力闸"，且要 promote 现有 tracer 而非另起炉灶。

推荐顺序（修订）：

1. **P-1 解耦 refactor**（compaction 出循环、message 列表单 owner、core 去 UI）——facade 的前置，abandon-safe。
2. **promote tracer/wire 为 `SessionEventStore`**（补 id/branch_id/turn_id/parent_id；定 wire 去向；durability/并发契约）。
3. **现有 emit 单写 fan-out 进 events**（非双写）。
4. **P0.5 PermissionEngine 合并**为单一 fail-closed 咽喉点 + 不变量测试。
5. EventSink 替代 core print（P2，低风险）。
6. ⚠ `AgentSession` + context builder（无其它迁移在飞时）。
7. branch/fork + compaction supersession 契约（P5）。
8. ☁ `AgentRuntime`/`RuntimeThread`、stdio App Server、Python SDK、extension 生态——**均 gating 在"出现第二个真实 client"**。

这样做的原因：

- 仓库已有事件 spine（tracer/wire），再起一条只会制造它要消灭的"缺统一事实源"。
- compaction 原地销毁历史，没有 supersession 事件，context builder/fork 只能做成伪实现。
- 没有 enforce 的 capability 闸，对外开 `turn.run` 是把本地能力面变成可远程触达的攻击面。
- facade（AgentRuntime/SDK）在只有 CLI 一个 in-process caller 时是过早 indirection，且不是 abandon-safe。

## 最小可行版本（收窄为"仅事件统一"）

如果只做一个小闭环，建议目标是：

```text
per-agent wire.jsonl 原地升级为 Pi-style entry tree（事实源 spine），统一流读时 merge，复用现有 nanocode trace replay/summary
```

具体包括：

- 给现有 emit 事件**加 envelope 字段**（`id=evt_{agent_id}_{seq}`/`parent_id`/`turn_id`/`branch_id`/`agent_id`），flat-additive、保留现有 type 名。✅ 已落地（`trace/tracer.py`）
- `seq` 从 wire tail 续号（resume-safe）；`turn_id` 在 `chat()` 起手铸。✅ 已落地（`engine._build_tracer` + `chat`）
- **不新增** session 根文件；统一时间线由读时 merge（`(ts,agent_id,seq,line_no)` 展示序）生成。✅ 已落地（`events/reader.merge_session_events`）
- 现有 `nanocode trace` replay/summary 可指向同一 wire 流——可审计性目标基本达成。✅ 已落地（`nanocode trace --wire`）
- **resume 仍以 snapshot 为权威**。✅ 未改 resume 路径
- 不做 AgentRuntime facade，不做 app-server，不做 fork，不做 SDK，不碰 PermissionEngine/P-1。✅

> **进度（2026-06-08，branch `feat/event-store`）**：MVP 三连已落地并通过交叉验证（workflow 4-lens + Codex CLI reviewer）。Codex 发现并已修复一个真实 bug（torn wire tail 续写会粘坏首个 resume 事件 → `JsonlSink` append 前补换行）；workflow 发现并已修复 emit envelope 权威性（payload kwarg 不能篡改 seq/id）。945 tests passing。下一步候选：P0.5 PermissionEngine 合并 / P-1 解耦 refactor（均需另起，spine 已自洽收口）。

> 注意：初版 MVP（events.jsonl + AgentRuntime in-process + TurnResult）其实捆绑了 P0+P1+P4，把一个 facade 重构塞进了"最小"里。真正的最小价值是可观测/可审计——只需事件统一一条 lane。AgentRuntime/TurnResult 等到真有第二个 caller 再做。

这个版本完成后，nanocode 的事实源从分散的三 lane + snapshot 收敛为一条，向 runtime-first 迈出第一步——且没有引入任何 abandon-unsafe 的 live-path 改动。

## 最终建议

nanocode 不应直接复制 Codex，也不应直接复制 Pi。更好的设计是：

```text
Codex-style embeddable runtime facade（aspirational，gating 在第二个 client）
  提供 thread/turn/approval/event stream/protocol/SDK

Pi-style event-sourced session runtime（先 promote 现有 tracer/wire，不另起 lane）
  提供 events.jsonl/session tree/fork/context builder/lifecycle events

nanocode-style safety core（先于平台面闭合）
  单一 fail-closed PermissionEngine 闸；保留现有 Python 可审计性、sandbox、permission、project trust、渐进式扩展
```

贯穿三者的一条硬约束：**任何未来的可嵌入 runtime / SDK / server 入口，都必须把每一次工具调用路由到与 CLI 相同的那一个 fail-closed 能力闸；绝不开第二个绕过它的入口。** 这是"可审计"在平台化之后仍然成立的前提。

这条路线能让 nanocode 从"一个可用 CLI agent"演进为"可嵌入、可恢复、可观察、可扩展的本地 agent runtime"，同时避免过早引入大而全插件生态和高风险任意代码扩展，也避免在单人维护下制造半成品迁移与冗余事实源。
