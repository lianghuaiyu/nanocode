# 15 · AgentCore + ContextRuntime + Multi-Agent 改造报告

日期：2026-06-11

状态：设计报告。本文基于当前工作区源码重新盘点；`docs/12`、`docs/13`、`docs/14` 只作为历史背景，若与当前源码或本文冲突，以当前源码和本文为准。

范围：Agent Core、canonical session tree、上下文工程、Aider-style repo map、subagent/multi-agent 预留、可嵌入式 runtime 分层。

## 0. 核心结论

nanocode 应采用 **Pi-style stateful `AgentCore + AgentSession + SessionManager`**，而不是一个极薄的 stateless executor。

关键不变量：

```text
SessionManager/session.jsonl = 唯一 durable truth
AgentCore.state              = 可丢弃的运行时投影
AgentSession                 = state <-> session tree 的同步边界
ContextRuntime               = 每次请求前的上下文工程层
RepoIndex/RepoMapProvider    = Aider-style code intelligence provider
AgentRuntime/TeamRuntime     = thread、child session、多 agent 协作编排层
```

一句话定调：

```text
Pi 管可信历史与状态重建；Claude Code 管请求时上下文工程；Aider 管代码库结构感知。
```

这意味着：

1. 可以直接使用 Pi 的 session tree 设计，而且应该使用。当前 nanocode 已经有 `session/tree.py`、`session/manager.py`、`session/context.py`、`session/render.py`，方向正确。
2. 不建议回到“比 Pi 更薄”的 AgentCore。流式、tool loop、abort、steer、follow-up、审批等待、compaction、pending tool result 都是运行态状态；把 core 做得过薄只会把复杂性挤到 `RuntimeThread` 或 backend mixin。
3. 但 Pi-style stateful core 必须受一个硬边界约束：**AgentCore 可以有状态，但 `AgentCore.state` 只是 `session.jsonl` 的 disposable projection，绝不能成为 durable truth。**
4. Claude Code 的上下文策略不能直接塞进 `prompt.py` 或 backend loop。它应落成独立 `ContextRuntime`：稳定 system/tools 前缀、动态 context pack、custom message 注入、budget ledger、cache stability policy 都在这里处理。
5. Aider 的 repo map 不应只是一个工具。它应成为 `RepoMapProvider`，被 `ContextRuntime` 根据任务、已读文件、用户提及符号、agent profile 自动注入。
6. subagent 不是普通工具调用的别名，而是 child `RuntimeThread` / child session。未来 multi-agent 协作应预留为 `TeamRuntime`，不应把共享任务板、agent-to-agent mailbox、claim lock 等设计混进 `agent` 工具内部。

## 1. 参考材料权威等级

### 1.1 已核对的 nanocode 当前源码

本报告直接读取当前工作区这些模块：

- `src/nanocode/session/tree.py`
- `src/nanocode/session/manager.py`
- `src/nanocode/session/context.py`
- `src/nanocode/session/render.py`
- `src/nanocode/session/capture.py`
- `src/nanocode/session/lease.py`
- `src/nanocode/agent/engine.py`
- `src/nanocode/agent/session.py`
- `src/nanocode/agent/runtime.py`
- `src/nanocode/agent/anthropic_backend.py`
- `src/nanocode/agent/openai_backend.py`
- `src/nanocode/subagents/config.py`
- `src/nanocode/tools/agent.py`
- `src/nanocode/tools/read_file.py`
- `src/nanocode/prompt.py`

### 1.2 外部参考

高权威：

- Pi docs: session format 与 SDK runtime/session API。
- Aider source/docs: `aider/repomap.py` 与 repo map 文档。
- Claude Code official docs: prompt caching、memory、subagents、agent teams。
- OpenCode docs: agents、primary/subagent、child session navigation、agent profile fields。

中权威：

- 本机安装的 `@anthropic-ai/claude-code@2.1.173` 包：已确认 npm 包主要是 `bin/claude.exe`、wrapper、声明文件，核心 TypeScript 源码不直接可读。
- `anthropics/claude-code` GitHub repo：公开 repo 有 README、plugins、examples 等，但不是完整 core 源码事实源。

低权威，但可作为实现线索：

- `pengchengneo/Claude-Code` 非官方还原源码。
- `https://diwang.info/how-claude-code-works/#/docs/03-context-engineering` 及其 raw markdown。它对 context.ts、system prompt boundary、cache、compact pipeline 的描述有工程价值，但不能替代官方行为契约。

## 2. 当前 nanocode 真实状态

### 2.1 已经做对的部分

session 层已经非常接近 Pi-style canonical tree：

- `session/tree.py` 有 `Entry`、`id/parentId/sessionId/type/timestamp/data` envelope、`leaf` entry、`FOLD_TYPES`、neutral message constructors。
- `session/manager.py` 已经有 `SessionManager.create/open/append/append_message/append_compaction/set_leaf/build_context/clone/children/parent_of`，并通过 `fcntl.flock` 做 writer lock。
- `session/context.py` 已经把 branch fold 成 rich messages + scalar state，并支持 compaction two-zone fold、`custom_message` 原样注入。
- `session/render.py` 已经把 neutral messages 渲染成 Anthropic/OpenAI payload，并做 thinking gate、image downgrade、aborted assistant drop、tool result orphan 处理、Anthropic tool_result merge、OpenAI tool role shaping。
- `session/capture.py` 已经把 provider-shaped message 捕获为 neutral message，并开始保留真实 stop/finish reason、usage、latency。
- `session/lease.py` 已经把 writer identity 从 `Agent.__init__` 中拿出来，改成 runtime active thread 持有 writer lease。

这些应该保留并上移为新架构的 L3 session truth，不要推翻。

### 2.2 仍然错误的中心

`src/nanocode/agent/engine.py` 仍是过重的事实中心。它同时持有：

- provider client、model、thinking、token counters；
- permission engine、confirm callbacks、plan mode；
- tools、MCP、skills、hooks、memory prefetch；
- `TaskManager`、background shell task、subagent manager；
- session lease、tree record、custom message injection；
- provider-local `_anthropic_messages` / `_openai_messages` projection；
- subagent factory、foreground/background subagent execution、memory curator/eval/optimize；
- artifacts、state cache、files read/modified derived facts。

当前代码虽然注释里多次强调 `_anthropic_messages` / `_openai_messages` 已降级为 turn-local projection，但 backend loop 仍直接 append、注入、compact、覆盖这些列表。它们不是 durable truth，但仍是太多运行逻辑的临时事实源。

这不是小修能解决的问题。`engine.py` 应被替换为 architecture center，而不是继续美化。

### 2.3 上下文层的主要问题

`src/nanocode/prompt.py` 仍在构造一个大而动态的 system prompt：

- cwd、date、platform、shell；
- git context；
- project instructions；
- memory section；
- skills guidance；
- agent descriptions；
- deferred tools。

这与 Claude Code-style context engineering 的核心原则冲突：稳定 system/tools 前缀要尽量不变，项目指令、memory、skill listing、repo map 等应作为 request-time context packs 或 `custom_message` 注入，而不是每次混进 system prompt。

backend loop 里还有这些上下文注入职责：

- memory prefetch settled 后写 `custom_message`；
- task completion reminder；
- skill listing delta；
- skill body pending injection；
- compaction summary；
- provider request sizing telemetry。

这些都应从 backend loop 移到 `ContextRuntime` 和 `AgentSession`。

### 2.4 code intelligence 的缺口

当前没有 Aider-style repo map / AST index：

- `rg`/`grep_search` 是工具层检索，不是 context provider。
- `read_file.py` 仍会整文件读取并加行号；`tools/shared.py` 有 `MAX_RESULT_CHARS=50000`，但 `read_file` 本身没有 line/byte cap。Aider/Pi 都强调大输出要在工具边界控量，不能只靠后续 compaction。
- 没有 tree-sitter tags、defs/refs graph、PageRank ranking、budgeted tree render、repo map cache。

这会导致模型在未读文件时缺少代码库结构感，也会导致为了理解结构而反复读大文件，污染上下文。

### 2.5 subagent 已有基础，但抽象不够

当前 `subagents/config.py` 支持：

- user/project custom agents；
- trust gate；
- built-ins: `explore`、`plan`、`general/coder`；
- `allowed-tools` / `disallowed-tools` / `extends`；
- `model`、`max-turns`、`timeout-ms`；
- 子 agent 有效工具集永远剔除 `agent`。

当前 `engine.py` 支持 foreground/background subagent、child session id、TaskManager 记录、结果 artifact、background completion injection。

但缺少正式的 `AgentProfile`：

- profile mode: `primary/subagent/system/all`；
- context profile；
- skills/MCP/memory/hook scope；
- isolation policy；
- permission derivation；
- hidden/system agents；
- result envelope schema；
- child runtime/thread lifecycle。

这些现在分散在 config dict、engine helper 和 TaskManager 派生 state 中。

## 3. 为什么选择 Pi-style stateful AgentCore

用户提出的关键问题是：既然 nanocode 已经能同步 session tree 和 Agent state，为什么不能直接用 Pi 的设计？

答案：可以，而且现在应当改为直接以 Pi-style stateful core 为目标。早先“更薄 core”的方案是为了避免旧 nanocode 把 durable truth 混在 `Agent` 中；在当前已引入 session tree 后，继续追求极薄 core 反而会错。

### 3.1 薄 core 的问题

一个真正可用的 coding agent loop 必然需要运行态状态：

- 当前是否 streaming；
- 当前 provider request；
- 当前 tool calls 与 pending tool results；
- abort/cancel 标记；
- steer/follow-up queue；
- 当前 model/thinking level；
- token/cost counters；
- approval wait；
- early tool execution tasks；
- pending context injections；
- compaction in progress；
- active tool list；
- per-turn event stream。

如果 `AgentCore` 完全 stateless，这些状态不会消失，只会跑到 `RuntimeThread`、backend mixin、global manager 或 callback closure 里，系统反而更难嵌入、测试和恢复。

### 3.2 Pi-style core 的正确边界

目标不是“AgentCore 无状态”，而是：

```text
AgentCore may be stateful, but AgentCore.state is disposable projection.
SessionManager owns durable truth.
```

具体边界：

- `AgentCore` 负责模型循环、流式消费、tool-call scheduling、事件发射。
- `AgentCore.state` 可以持有 messages/tools/model/system/runtime counters，但必须能从 `SessionManager.build_context()` + `AgentProfile` + runtime config 重建。
- `AgentCore` 不直接写 session 文件。
- `AgentCore` 不直接读项目文件、memory、skills、MCP config、repo map。
- `AgentCore` 不持有 provider-specific durable messages。
- `AgentCore` 发出 `AgentEvent`，由 `AgentSession` 记录成 session entries 或 telemetry entries。

### 3.3 AgentSession 是同步边界

`AgentSession` 应从当前薄包装升级为真正的 session lifecycle owner：

```text
AgentSession.start_turn(user_input)
  -> append user message entry
  -> build ContextRequest
  -> hydrate AgentState from tree + profile + context
  -> run AgentCore
  -> record AgentEvents to session tree
  -> run compaction/context hooks
  -> verify turn consistency
```

职责：

- 从 session tree hydrate `AgentCore.state`；
- 把 `AgentEvent` 写回 canonical entries；
- 管理 turn boundary；
- 管理 tree navigation；
- 管理 compaction entry；
- 管理 custom_message entry；
- 管理 provider render legality；
- turn end 做一致性检查。

## 4. 目标分层

目标仍然是可嵌入式 agent，不是单 CLI。

```text
Layer 7  Clients
         CLI / Python SDK / IDE / Web UI / CI

Layer 6  Protocol Adapters
         JSON-RPC / stdio / socket / websocket

Layer 5  AppServer
         process boundary, request router, event fanout, approval parking

Layer 4  Runtime
         AgentRuntime, RuntimeThread, TeamRuntime, spawn/rebind/cancel/events

Layer 3  Session Runtime
         SessionManager, AgentSession, session tree, branch/fork/clone/render

Layer 2  Agent Core
         AgentCore, AgentState, AgentEvent, provider loop, tool scheduling

Layer 1  Context + Capabilities
         ContextRuntime, RepoIndex, CapabilityRouter, permissions, tools, MCP, skills, memory

Layer 0  Platform Adapters
         Anthropic/OpenAI SDK, filesystem, shell sandbox, terminal/browser adapters
```

注意：`ContextRuntime` 放在 L1/L2 之间的横切层。它不应该写 session，也不应该调用 provider；它只把可用上下文来源按预算和缓存策略组装成 `ContextPlan`。

## 5. 目标模块拆分

建议直接新增/重排，不做迁移期兼容层。

```text
src/nanocode/agent/
  core.py            # AgentCore: model loop + tool scheduling
  state.py           # AgentState, ProviderProjection, TurnState
  events.py          # AgentEvent union
  loop.py            # provider-independent loop helpers
  providers.py       # Anthropic/OpenAI adapters behind one interface

src/nanocode/session/
  manager.py         # canonical storage, writer lock
  agent_session.py   # state <-> tree synchronization
  tree.py            # pure tree data/functions
  context.py         # branch fold
  render.py          # neutral -> provider payload
  capture.py         # provider output -> neutral facts

src/nanocode/context/
  runtime.py         # ContextRuntime
  ledger.py          # ContextLedger and accounting
  providers.py       # ContextProvider protocol
  budgets.py         # BudgetPolicy
  packs.py           # ContextPack
  cache_policy.py    # prompt cache stability policy

src/nanocode/codeintel/
  index.py           # RepoIndex facade
  symbols.py         # tree-sitter tags: defs/refs
  graph.py           # dependency graph/PageRank
  repomap.py         # budgeted repo-map rendering
  cache.py           # mtime/content-hash cache

src/nanocode/agents/
  profile.py         # AgentProfile
  registry.py        # user/project/plugin/builtin discovery
  builtin.py         # build/plan/explore/general/system agents
  permissions.py     # profile permission derivation

src/nanocode/capabilities/
  router.py          # single dispatch for tools/MCP/skills/subagents
  tools.py
  mcp.py
  skills.py
  subagents.py       # thin adapter to runtime.spawn_child
  permissions.py

src/nanocode/runtime/
  runtime.py         # AgentRuntime
  thread.py          # RuntimeThread
  spawn.py           # child sessions/subagents
  teams.py           # future TeamRuntime
  approvals.py
  events.py
```

After this split, `agent/engine.py` should disappear as the architecture center. Since this is a major rewrite, do not keep fallback paths for old `messages.json` authority or old flat provider lists.

## 6. AgentCore contract

`AgentCore` should expose a small but stateful API:

```python
class AgentCore:
    def __init__(self, provider: ProviderAdapter, capabilities: CapabilityRouter, sink: EventSink): ...

    async def run_turn(self, state: AgentState, request: TurnRequest) -> AsyncIterator[AgentEvent]:
        ...

    async def abort(self) -> None: ...
    async def steer(self, message: str) -> None: ...
    async def follow_up(self, message: str) -> None: ...
```

`AgentState` includes:

- neutral messages for the active branch;
- provider projection for the current request only;
- model/provider/thinking settings;
- active tool definitions;
- pending tool calls/results;
- abort/streaming flags;
- token/cost counters;
- pending approvals;
- turn-local context ledger snapshot.

`AgentEvent` includes:

- `UserMessageAccepted`;
- `LlmRequestPrepared`;
- `AssistantDelta`;
- `AssistantMessageCompleted`;
- `ToolCallRequested`;
- `ToolCallAuthorized`;
- `ToolResultCompleted`;
- `ToolBlocked`;
- `CompactionRequested`;
- `ContextInjected`;
- `TurnCompleted`;
- `TurnAborted`;
- `ErrorRaised`.

Hard no:

- `AgentCore` 不调用 `SessionManager.append_*`。
- `AgentCore` 不调用 `build_system_prompt()`。
- `AgentCore` 不直接发现 skills/subagents/MCP。
- `AgentCore` 不直接写 artifacts。
- `AgentCore` 不维护 durable provider messages。

## 7. AgentSession contract

`AgentSession` should become the only object that knows both `SessionManager` and `AgentCore`.

```python
class AgentSession:
    async def run_turn(self, prompt: str) -> TurnResult: ...
    async def compact(self, instructions: str | None = None) -> CompactionResult: ...
    def navigate_tree(self, target_id: str | None) -> None: ...
    def hydrate_state(self) -> AgentState: ...
    def record_event(self, event: AgentEvent) -> None: ...
```

Responsibilities:

1. Append user message to canonical tree before model request.
2. Ask `ContextRuntime` for context packs.
3. Build `AgentState` from:
   - `SessionManager.build_context()`;
   - active `AgentProfile`;
   - current runtime config;
   - context packs.
4. Run `AgentCore`.
5. Convert events into:
   - `message`;
   - `custom_message`;
   - `compaction`;
   - `model_change`;
   - `thinking_level_change`;
   - `active_tools_change`;
   - `permission_decision`;
   - `task_update`;
   - telemetry entries.
6. Turn-end consistency:
   - every assistant tool call has a tool result or synthetic no-result;
   - aborted assistant is marked and render can drop it;
   - no orphan provider payload;
   - session leaf points to the last leaf-affecting entry of this turn;
   - `AgentCore.state` can be rebuilt from tree.

## 8. ContextRuntime: Claude Code-style 上下文工程

Current `prompt.py` should be broken apart. The new rule:

```text
system prompt = stable identity and behavior rules
tools         = stable capability schemas, ordered for cache
messages      = canonical branch + request-time context packs
```

### 8.1 ContextPack

Every injected context source becomes a structured pack:

```python
@dataclass
class ContextPack:
    id: str
    kind: str
    content: str | list[dict]
    lifecycle: Literal["session", "turn", "until_compact", "path_triggered", "manual"]
    provenance: dict
    token_estimate: int
    priority: int
    cache_policy: Literal["stable_prefix", "append_only", "volatile_tail"]
    persist_policy: Literal["none", "custom_message", "message", "derived_only"]
```

Examples:

- project root instructions;
- nested/path-scoped instructions;
- current date;
- git snapshot;
- memory recall;
- skill listing delta;
- skill body;
- MCP/deferred tool announcements;
- repo map;
- background task completion;
- file changed reminder;
- compaction summary;
- agent profile instructions.

### 8.2 ContextLedger

`ContextLedger` should answer `/context` and drive budget decisions:

- which packs are present;
- how many tokens each pack costs;
- source/provenance;
- why included;
- when it expires;
- whether it survives compaction;
- whether it is persisted to session tree;
- whether it breaks provider cache.

This is the missing abstraction in nanocode today. Without it, context quality cannot be debugged.

### 8.3 Prompt cache policy

Borrow the Claude Code idea:

- Stable global system text first.
- Tool schemas ordered deterministically.
- Dynamic system text minimized.
- Project/user context as messages or custom messages, not mutable system text.
- Skills, commands, repo map, memories, task reminders appended as conversation/context packs.
- Model/thinking/tool-set changes are explicit cache-breaking events.
- Permission mode changes should not rewrite tool schemas unless a whole tool is removed.

For Anthropic, keep provider-specific cache controls in provider adapter, not in context providers.

### 8.4 Compaction survival matrix

Define survival explicitly:

| Context source | After compact |
|---|---|
| stable system prompt | unchanged |
| tool schemas | unchanged unless tool set changes |
| root project instructions | reloaded as session pack |
| path-scoped instructions | lost until path trigger fires again |
| memory recall | re-evaluated or reloaded under memory budget |
| skill listing | re-emitted as delta if needed |
| skill body | retained under per-skill and total budget |
| repo map | recomputed from current branch/request |
| tool outputs | summarized or capped at tool boundary |
| subagent transcript | never merged; result envelope remains |

### 8.5 `custom_message` remains important

Pi's `custom_message` maps well to Claude-style `<system-reminder>` attachments. Use it for context that should participate in LLM context but remain typed/auditable:

- memory injections;
- skill listing/body;
- task completion reminders;
- repo map snapshots if they should be replayable;
- file changed reminders;
- background subagent completion summaries.

Do not mutate the last user message in place. That breaks reconstruction and cache reasoning.

## 9. Aider RepoMap / AST 改造

Aider's `RepoMap` has the useful shape nanocode lacks:

- tree-sitter tags;
- definition/reference extraction;
- fallback references via lexical tokens when needed;
- graph of referencer -> definer;
- PageRank with personalization from chat files, mentioned filenames, mentioned identifiers;
- token-budgeted rendering by binary search over ranked tags;
- cache keyed by file mtime and map request;
- tree-context rendering around lines of interest.

### 9.1 Target `RepoIndex`

```python
class RepoIndex:
    def update(files: Iterable[Path]) -> None: ...
    def tags(file: Path) -> list[SymbolTag]: ...
    def rank(query: RepoQuery) -> RankedRepoMap: ...
    def render(map: RankedRepoMap, budget: TokenBudget) -> str: ...
```

`SymbolTag`:

```python
@dataclass(frozen=True)
class SymbolTag:
    rel_path: str
    abs_path: str
    line: int
    name: str
    kind: Literal["def", "ref"]
    language: str
```

`RepoQuery`:

- active chat files;
- files read this session;
- files modified this session;
- user mentioned file names;
- user mentioned identifiers;
- traceback/test failure symbols;
- current task intent;
- agent profile context mode.

### 9.2 `RepoMapProvider`

`RepoMapProvider` is a `ContextProvider`, not a tool:

```python
class RepoMapProvider(ContextProvider):
    async def collect(self, request: ContextRequest) -> ContextPack | None:
        ...
```

It should:

- skip when agent profile disables codeintel;
- use larger budget when no files have been read yet;
- shrink when active files are already in context;
- include top-level repo map early in a task;
- refresh when mentioned identifiers or modified files change;
- never include full files unless explicitly requested.

### 9.3 Tool boundary must still cap output

Repo map does not replace tool output governance. `read_file.py` should gain:

- byte cap;
- line cap;
- offset/range support;
- clear truncation marker;
- optional full-read-to-artifact path;
- integration with `ContextLedger`.

Do this before removing old compression protections. Otherwise a single large file read can still damage the context before compaction can help.

## 10. AgentProfile: subagent 和多 agent 的基础

Replace dict configs from `subagents/config.py` with a formal profile:

```python
@dataclass
class AgentProfile:
    name: str
    description: str
    mode: Literal["primary", "subagent", "system", "all"]
    prompt: str
    model: str | None
    thinking: str | None
    temperature: float | None
    top_p: float | None
    max_turns: int | None
    timeout_ms: int | None
    tools_allow: set[str] | None
    tools_deny: set[str]
    spawn_allow: set[str] | None
    permission: PermissionProfile
    context: ContextProfile
    skills: list[str]
    mcp_servers: list[McpServerRef]
    memory: MemoryPolicy
    hooks: HookPolicy
    isolation: IsolationPolicy
    hidden: bool = False
```

Built-ins:

- `build` / main write-capable primary;
- `plan` primary with write/shell restricted;
- `explore` read-only subagent;
- `general` write-capable subagent;
- `repo-map` or `scout` read-only docs/dependency research;
- `compaction` hidden system agent;
- `title` hidden system agent;
- `summary` hidden system agent;
- memory curator/eval hidden system agents.

Claude Code and OpenCode both point in this direction: agents are profiles with mode/model/tools/permissions/context behavior, not just prompts.

## 11. Subagent runtime model

The `agent` tool should become a thin adapter:

```text
AgentCore tool call
  -> CapabilityRouter sees tool "agent"
  -> AgentRuntime.spawn_child(parent_thread, AgentProfile, prompt, options)
  -> child RuntimeThread with child SessionManager
  -> parent receives bounded result envelope
```

### 11.1 Child session

Each child is a normal session:

```text
~/.nanocode/sessions/<parent>.<child>/
  session.jsonl
```

Child `session_start` carries:

```json
{
  "parentSession": {
    "sessionId": "parent",
    "entryId": "spawn_entry",
    "toolCallId": "toolu_...",
    "agentId": "agent_..."
  },
  "agent": {
    "name": "explore",
    "mode": "subagent",
    "background": true
  }
}
```

Parent branch stores only:

- tool call arguments;
- running/completed result envelope;
- result artifact path;
- child session id;
- files read/modified derived by host;
- token usage;
- status/error.

Never fold child transcript into parent context.

### 11.2 Foreground vs background

Foreground:

- parent waits;
- child completes;
- parent gets bounded result envelope;
- child transcript remains in child session.

Background:

- parent immediately gets `running` envelope;
- child runs under its own writer lease;
- completion writes child terminal state;
- parent receives a `custom_message` reminder on the correct branch or a queued notification for next turn.

### 11.3 Permission inheritance

Rules:

- child cannot exceed parent policy;
- deny sets union;
- allow sets intersect when both exist;
- background child auto-denies interactive approvals;
- confirmed paths do not flow from background child back to parent;
- child cannot spawn descendants unless parent/profile explicitly allows and global depth budget permits.

## 12. Future TeamRuntime

Do not implement multi-agent cooperation inside `agent` tool. Reserve a separate runtime:

```text
TeamRuntime
  TeamSession
  TeamTaskBoard
  AgentMailbox
  ClaimLock
  SharedArtifactStore
  TeamEventStream
```

Subagents and teams differ:

| Dimension | Subagent | TeamRuntime |
|---|---|---|
| Coordination | parent delegates | shared task board |
| Communication | child reports to parent | agents message each other |
| Context | isolated child context | isolated peer contexts |
| Cost | lower | higher |
| Use case | focused task | collaborative investigation/build |

Reserve session entries now:

- `team_start`;
- `team_task_update`;
- `team_message`;
- `team_claim`;
- `team_result`;
- `agent_mailbox_message`.

These can be typed `custom` entries initially if the schema is not ready, but do not overload `task_update` or parent tool results.

## 13. Impact of changing Agent Core

Changing Agent Core touches most of the runtime surface. Expected blast radius:

1. Backend mixins disappear or become provider adapters.
2. `_anthropic_messages` / `_openai_messages` disappear from `Agent` and become request-local projections.
3. `_tree_record()` moves to `AgentSession.record_event()`.
4. `_build_request_messages()` becomes `ContextRuntime + session.render`.
5. `prompt.py` is split into stable prompt template and context providers.
6. `CapabilityRouter` owns tools/MCP/skill/subagent dispatch.
7. `TaskManager` becomes runtime/session derived state, not core state.
8. `SubAgentManager` becomes runtime spawn policy.
9. CLI no longer calls `Agent` private methods; it calls `RuntimeThread`.
10. Tests shift from asserting private list mutations to asserting session entries and emitted events.

This is a large rewrite, but it removes the current hidden coupling instead of preserving it.

## 14. What to delete, not preserve

Because this is a major refactor and migration compatibility is not required:

- delete runtime fallback to old flat `messages.json` authority;
- delete provider list as resume/request authority;
- delete “tree write failed then mutate flat list” fallback;
- delete command paths that mutate `agent._session_mgr` directly;
- delete dynamic all-in-one `build_system_prompt()` as the main context source;
- delete subagent config dict as the long-term profile API;
- delete tool/result injection by in-place mutation of last user message;
- delete legacy assumptions that `TaskManager`/v2 state is authoritative.

Allowed caches:

- repo index cache;
- rendered provider payload cache;
- context pack cache;
- task/subagent derived display cache;
- title/name cache.

But every cache must be derivable from canonical tree + runtime config + filesystem state where appropriate.

## 15. Implementation plan

### Phase 0: Schemas and tests first

Add:

- `agent/state.py`;
- `agent/events.py`;
- `agents/profile.py`;
- `context/packs.py`;
- `context/ledger.py`;
- `context/providers.py`;
- `codeintel/symbols.py`;
- contract tests for tree hydrate/render.

Acceptance:

- a neutral branch can hydrate `AgentState`;
- `AgentState` can render Anthropic/OpenAI requests;
- no provider-specific durable messages are needed.

### Phase 1: Extract AgentCore

Move model loop into `AgentCore`.

Acceptance:

- existing simple chat works through `AgentSession.run_turn`;
- `AgentCore` emits events but does not write session;
- abort still cancels streaming/tool loop correctly.

### Phase 2: AgentSession owns persistence

Move `_tree_record`, `_tree_event`, `_tree_custom_message`, compaction entry writes into `AgentSession`.

Acceptance:

- every user/assistant/tool result is a session entry;
- turn replay rebuilds same `AgentState`;
- no `engine.py` flat fallback.

### Phase 3: ContextRuntime

Split `prompt.py` into:

- stable system prompt;
- project instructions provider;
- git snapshot provider;
- memory provider;
- skill provider;
- task reminder provider;
- repo map provider placeholder.

Acceptance:

- `/context` can show included packs and budgets;
- project instructions and memory are not baked into mutable system prompt;
- compact has a survival matrix.

### Phase 4: RepoIndex / RepoMapProvider

Implement Aider-style minimum:

- tree-sitter tags for supported languages;
- defs/refs graph;
- PageRank ranking;
- mtime cache;
- budgeted render;
- context provider integration.

Acceptance:

- first turn can include a bounded repo map;
- mentioned symbol changes ranking;
- modified/read files affect personalization;
- output respects budget.

### Phase 5: CapabilityRouter and AgentProfile

Move tool/MCP/skill/subagent dispatch out of core.

Acceptance:

- all tool calls pass one router;
- subagent effective permissions derive from parent/profile;
- profiles cover current built-ins and custom agents.

### Phase 6: Runtime spawn child

Move subagent foreground/background execution to `AgentRuntime.spawn_child`.

Acceptance:

- child has independent session tree;
- parent stores bounded result envelope;
- background completion does not pollute unrelated branches;
- child transcript can be opened independently.

### Phase 7: CLI as runtime client

CLI owns no private core mutation.

Acceptance:

- `/new`, `/resume`, `/fork`, `/clone`, `/tree`, `/agents`, `/context` go through runtime/session APIs;
- `RuntimeThread.events()` can drive CLI output;
- approval requests carry thread/agent identity.

### Phase 8: TeamRuntime skeleton

Do not build full teams yet. Add skeleton interfaces and session entry types.

Acceptance:

- can create a team session with a task board;
- no agent-to-agent communication in parent transcript;
- future implementation has a reserved state model.

## 16. Test strategy

High-value tests:

- `tests/session/test_rebuild_state.py`: session tree -> `AgentState` exactly.
- `tests/session/test_render_legality.py`: aborted/orphan/tool result cases.
- `tests/context/test_ledger.py`: budget/provenance/survival matrix.
- `tests/context/test_prompt_cache_policy.py`: dynamic packs append without rewriting stable system.
- `tests/codeintel/test_repomap_rank.py`: defs/refs ranking and mentioned identifier personalization.
- `tests/tools/test_read_file_budget.py`: range/cap/truncation.
- `tests/runtime/test_child_session.py`: foreground/background child session and result envelope.
- `tests/runtime/test_rebind.py`: runtime replacement does not mutate core private fields.
- `tests/agents/test_profile_permissions.py`: allow/deny inheritance.

## 17. Open decisions

1. Whether `session_start` remains a leaf-affecting entry. Current code treats it as non-leaf-affecting in `leaf_id_after_entry`; keep that unless tests show a branch root issue.
2. Whether repo map snapshots should persist as `custom_message` or be request-ephemeral. Recommendation: ephemeral by default, persist only when it materially influenced the model or user asks for reproducibility.
3. Whether path-scoped project instructions should be `custom_message` entries or ephemeral packs. Recommendation: `custom_message` after first trigger, because the model did see them and replay should preserve that fact.
4. How to identify child sessions: current `<parent>.<agent_id>` is workable but may be too coupled to display id. Prefer minted child session id plus parent backlink for future teams.
5. How much of OpenCode-style primary agent switching to expose in CLI. The runtime should support it, but CLI can start with `/agent switch`.

## 18. Source links

- Aider repo map docs: https://aider.chat/docs/repomap.html
- Aider `repomap.py`: https://github.com/Aider-AI/aider/blob/main/aider/repomap.py
- Pi session format: https://pi.dev/docs/latest/session-format
- Pi SDK / `AgentSessionRuntime`: https://pi.dev/docs/latest/sdk
- Claude Code prompt caching: https://code.claude.com/docs/en/prompt-caching
- Claude Code memory: https://code.claude.com/docs/en/memory
- Claude Code subagents: https://code.claude.com/docs/en/sub-agents
- Claude Code agent teams: https://code.claude.com/docs/en/agent-teams
- Claude Code official repo: https://github.com/anthropics/claude-code
- Claude Code public context-engineering analysis: https://diwang.info/how-claude-code-works/#/docs/03-context-engineering
- Raw context-engineering markdown: https://raw.githubusercontent.com/Windy3f3f3f3f/how-claude-code-works/main/docs/03-context-engineering.md
- OpenCode agents docs: https://opencode.ai/docs/agents/
