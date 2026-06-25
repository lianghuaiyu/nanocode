# 24 · ToolRegistry 能力化与工具边界统一方案

> 目标：在 0 上下文环境下，按本文即可把 nanocode 的工具系统从「硬编码 + host-routed 旁路」收敛为
> 「能力化、最小授权、可嵌入但不破边界」的单一注册表 + dispatch 咽喉点。
>
> 结论先行：工具 = 自带行为的单元(`execute(input, ctx)`)，进**一张开放注册表**；执行环境(沙箱中介的
> exec/fs/spawn/…)在 runtime 层**注入一次**；dispatch 在咽喉点授权后，按「工具声明的能力 ∩ 其信任档允许
> 的能力」**现场铸造** per-call 能力把手塞进 `ToolContext` 再执行。`ToolContext` 无任何字段通向 raw
> `Agent`/`SessionManager`/`lease`——这是 Codex `pub(crate)` 在 Python 里的等价物。

## 0. 设计愿景与定位

- **内部像 Pi**：工具自包含(`execute` 闭包)、单一注册表、内置工具由工厂构造并注入能力。
- **外部像 Codex 可嵌入**：公开句柄不暴露内核；嵌入者经前门注册工具，但拿不到 raw 内核。
- **保证嵌入式边界(第一优先)**：跨边界角色(外部句柄 / 扩展 / 工具)只拿 curated 能力面，内核只在
  harness 内可达。
- **OpenCode 借鉴**：`ctx.ask` 运行中确认、传 ID 不传对象、按 agent/model 过滤工具。

> 「内 Pi / 外 Codex」是参考而非教条；本文在三家之上做了取舍，最终原则是 **capability-based 最小授权**
> + **工具信任分档**。

## 1. 统一原则：边界 = curated 能力面，按信任分档授予

一条贯穿性原则(与 docs/23 同源)：

> 凡跨边界的角色都只拿到一个 curated 能力面，绝不拿到 raw 内核对象；内核(`Agent` /
> `SessionManager` / `SessionLease`)只在 harness 内部可达。

工具是这条原则的第三处应用(前两处：外部 `RuntimeThread` 句柄、扩展 `ExtensionContext`)。三者同构。

**信任三档(上游开关，已拍定为「默认不可信、可信 opt-in」)：**

| 档 | 谁 | 能力授予 |
| --- | --- | --- |
| `BUILTIN` | 仓内自带工具 | 声明什么给什么(把手仍沙箱中介) |
| `TRUSTED` | 显式 opt-in 的第一方扩展 / SDK 嵌入者 | 可申请敏感能力(spawn/memory…)，过策略 |
| `UNTRUSTED`(默认) | 扩展 / MCP / 嵌入者注册的工具 | 仅受限能力集，全沙箱中介，全须策略放行；不可 shadow 内置 |

## 2. 非目标

- 不为白盒/旧调用保留 `tools/execute.py` 的 `_HANDLERS` 与 `tools/registry.py` 的 `tool_definitions` 两个派生全局(收口进 registry)。
- 不保留 MCP 的 `is_mcp_tool` 旁路(并入同一张表)。
- 不把 OpenCode 的 Effect/AI-SDK 工具包装、Codex 的「每轮全量重建注册表」照搬进来。
- 不把权限下推进工具函数(裁决留咽喉点)。
- 不引入 feature-flag 双路径；激进直切(见 [[aggressive-refactor-no-compat]] 精神)。

## 3. 参考源码总表

### 3.1 nanocode 当前源码

| 主题 | 源码 | 说明 |
| --- | --- | --- |
| 单一真相源已存在 | `src/nanocode/tools/spec.py`(`ToolSpec`/`_ALL`/`TOOLS`) | schema+run+元数据已合一；`tool_definitions`/`_HANDLERS` 均派生自 `TOOLS`。 |
| host-routed 旁路 | `tools/spec.py`(`run=None` 工具：agent/skill/run_shell/memory/tasks/plan/tool_search) | 行为不在工具里，散在 router/engine 分支按名派发——**非自包含**。 |
| dispatch 咽喉点 | `src/nanocode/capabilities/router.py:85`(`dispatch(host, name, inp)`) | 已传 curated `host: ToolHost`；权限/allowlist 留在此点(不下推)。 |
| 注册表是封闭的 | `tools/spec.py`(`_ALL` 模块全局) | 无 `register()`、无 `AgentConfig.tools`。 |
| 扩展 register_tool 被禁 | `src/nanocode/extensions/api.py:47` | 抛错；注释明确「未来须经 CapabilityRouter/PermissionEngine 包装」。 |
| 工具集合可实例级覆盖 | `src/nanocode/agent/engine.py`(`custom_tools`) | 子 agent 用，但只能子集化既有 schema。 |
| bootstrap 无注入 slot | `src/nanocode/runtime/facade.py:127`(`AgentConfig`) | 只带标量，不带 tools/能力注入口。 |
| 审批 broker 已有 | `src/nanocode/runtime/facade.py`(`RuntimeApprovalBroker`) | 异步 request/response，可复用作工具审批回路。 |

### 3.2 Pi 参考(`/Users/jyxc-dz-0101321/Projects/agent-reference-repos/pi`)

| 主题 | 源码 | 可借鉴点 |
| --- | --- | --- |
| 自包含工具契约 | `packages/agent/src/types.ts:366`(`AgentTool`)、`:375`(`execute(toolCallId, params, signal, onUpdate)`) | 工具自带 execute；per-call 极简。 |
| 结构化结果 | `packages/agent/src/types.ts:345`(`AgentToolResult{content, details, terminate?}`) | content 回模型 / details 进 UI / terminate 提前停。 |
| 内置工具工厂注入能力 | `packages/coding-agent/src/core/tools/index.ts`(`createBashTool({operations})`) | fs/shell 在构造时注入——ExecutionEnv 落到工具粒度。 |
| 定义→运行时包装 + ctxFactory | `packages/coding-agent/src/core/tools/tool-definition-wrapper.ts`(`wrapToolDefinition(def, ctxFactory)`) | 扩展工具经 ctxFactory 拿 curated ctx。 |
| 单一注册表汇合 | `packages/coding-agent/src/core/agent-session.ts:2299`(`_refreshToolRegistry`) | base + 扩展 + SDK 汇成一张 `Map<name, AgentTool>`，选活跃子集。 |
| 注意：允许按名覆盖内置 | `agent-session.ts:2326-2331`(`definitionRegistry.set(name,…)` 后者赢) | 对可信扩展是 feature；**对不可信是后门**——本文不采纳。 |

### 3.3 Codex 参考(`/Users/jyxc-dz-0101321/Projects/agent-reference-repos/openai-codex`)

| 主题 | 源码 | 可借鉴点 |
| --- | --- | --- |
| 注册表 + dup 拒绝 | `codex-rs/core/src/tools/registry.rs`(`ToolRegistry{tools: HashMap<ToolName, Arc<dyn CoreToolRuntime>>}`、`from_tools` 重名报错) | 名→运行时映射，构造期防撞。 |
| 命名空间 | `codex-rs/core/src/tools/mod.rs:39`(`flat_tool_name` = namespace+name)、`ToolName{namespace, name}` | MCP/app 工具带 namespace，对模型展平。 |
| 解析 + 派发 | `codex-rs/core/src/tools/router.rs`(`ToolRouter` 解析 `ToolName` → dispatch) | 模型 function call → 命名空间工具。 |
| 异步审批 | `protocol`(`EventMsg::ExecApprovalRequest`) + `Op::ExecApproval{decision}` | 审批事件出 / 决定 op 入，解耦——对位 `RuntimeApprovalBroker`。 |
| 工具皆一等公民(无用户插件) | `tools/handlers/*` | Codex 工具全在 crate 内可信；无「不可信工具 ctx」参考。 |

### 3.4 OpenCode 参考(`/Users/jyxc-dz-0101321/Projects/agent-reference-repos/opencode`)

| 主题 | 源码 | 可借鉴点 |
| --- | --- | --- |
| 工具 ctx 传 ID 不传对象 | `packages/plugin/src/tool.ts`(`Tool.Context{sessionID, abort, metadata(), ask()}`) | 给 sessionID 字符串而非 Session 对象——强边界。 |
| 注册 + 按 agent/model 过滤 | `packages/opencode/src/tool/registry.ts`(`builtin` + `fromPlugin`、`tools({providerID, modelID, agent})`) | 能力按上下文过滤。 |
| 执行包装 + 权限 ask | `packages/opencode/src/session/tools.ts`(`resolve` 包 execute，`tool.execute.before/after` 钩子、`ctx.ask`) | 运行中确认。 |
| 阻塞式审批握手 | `packages/opencode/src/permission/index.ts`(`Permission.ask` 阻塞 Deferred + 发 `permission.asked`) | ask 回路。 |

## 4. 目标架构

### 4.1 核心数据结构

```python
class Capability(Enum):
    EXEC = "exec"; FS_READ = "fs:read"; FS_WRITE = "fs:write"
    SPAWN = "spawn"; MEMORY = "memory"; TASKS = "tasks"
    SESSION_READ = "session:read"; MODELS = "models"; SET_MODE = "set_mode"
    # emit / ask / abort 属 per-call 核心，人人都有，不在声明里。

class Trust(Enum):
    BUILTIN = "builtin"; TRUSTED = "trusted"; UNTRUSTED = "untrusted"

@dataclass(frozen=True)
class ToolResult:
    content: list[Block]          # 回模型
    details: dict | None = None   # UI/审计，不进模型上下文
    is_error: bool = False
    terminate: bool = False       # 本批工具后提前停(Pi AgentToolResult 形)

@dataclass(frozen=True)
class Tool:
    name: str
    schema: dict                                   # 模型可见入参(闭合 additionalProperties=false)
    execute: "Callable[[dict, ToolContext], Awaitable[ToolResult]]"
    needs: frozenset[Capability] = frozenset()     # 能力声明(最小授权核心)
    source: ToolSource = ToolSource.BUILTIN        # builtin | mcp:<server> | ext:<id> | embedder
    trust: Trust = Trust.BUILTIN
    concurrency: Literal["sequential", "parallel"] = "sequential"
    deferred: bool = False
```

### 4.2 ToolContext = per-call 核心 + 按需铸造的能力把手

```python
@dataclass(frozen=True)
class ToolContext:
    # 核心：人人都有，全是"自己的"东西，够不到内核
    call_id: str
    cwd: str
    signal: AbortSignal
    emit: Callable[[Event], None]            # 进度/通知(不入树)
    ask:  Callable[[str], Awaitable[bool]]   # 运行中确认(只能在已授权范围内问)
    # 能力：仅当 tool.needs 声明 且 策略对该 trust 放行才铸造把手，否则 None
    exec:     "ExecCap | None"               # 沙箱中介进程执行
    fs_read:  "FsReadCap | None"
    fs_write: "FsWriteCap | None"            # 强制 sandbox writable_roots
    spawn:    "SpawnCap | None"              # 受控子 agent(bounded)
    memory:   "MemoryService | None"
    tasks:    "TaskView | None"
    session:  "ReadOnlySessionView | None"
    models:   "ModelRouter | None"
    set_mode: "Callable | None"
    # 无 .agent / ._session_mgr / .lease —— 物理上够不到内核(边界命根子)
```

铸造规则：`UNTRUSTED` 工具即便声明 `needs={MEMORY, SPAWN}`，若策略不放行，槽即 `None`——运行时拿到空，越不了权。`BUILTIN` 声明什么铸什么(把手仍沙箱中介)。

### 4.3 注册表

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None:
        # 外部工具(非 BUILTIN)强制 namespace；撞保留/内置名 → fail-loud；
        # 覆盖内置 → 仅 TRUSTED 且显式 override=True。
    def get(self, name: str) -> Tool | None: ...
    def schemas(self, active: set[str] | None = None) -> list[dict]: ...  # provider 只见 schema
    def overlay(self, extra: list[Tool]) -> "ToolRegistry": ...           # 每 agent/子 agent 叠加
```

### 4.4 Dispatch 流水线(纵深防御，真保证在沙箱中介能力层)

```text
模型 tool_call(name, args)
  ① 解析 + 防撞：registry.get(name)；外部名带 namespace；撞保留名 → 拒
  ② 咽喉点预授权：PermissionEngine + allowlist + sandbox policy
        → allow / deny / ask(异步经 RuntimeApprovalBroker 回 decision)
        deny → 回错误，不进 execute
  ③ 铸造 ToolContext：核心 + { 对每个 c ∈ tool.needs：policy_allows(c, tool.trust)
        则铸造沙箱中介把手，否则 None }
  ④ await tool.execute(args, ctx)        ← 工具只能用 ctx 里有的把手
        ★真保证：exec/fs 把手本身沙箱中介，工具恶意/忘记 ask 也越不了权
  ⑤ ToolResult → content 回模型 / details 进 UI·审计
```

权限三层：**②咽喉点裁决** + **③④沙箱中介能力(不依赖工具配合)** + **`ctx.ask` 可选合作层**。

### 4.5 四条注册路径，一个 `register()`

| 来源 | 路径 | trust | namespace |
| --- | --- | --- | --- |
| 内置 | 启动时 `register()` | BUILTIN | 无(保留名) |
| MCP | mcp_manager 发现 → `register()` | UNTRUSTED | `mcp__<server>__name` |
| 扩展 | `api.register_tool(schema, handler, needs=…)` | UNTRUSTED(可 opt-in TRUSTED) | `ext__<id>__name` |
| 嵌入者 | `AgentConfig.tools` / `runtime.register_tool(…)` | UNTRUSTED(可 opt-in TRUSTED) | `embedder__name` |

殊途同归：都造 `Tool` 进同一张表，都被同一 dispatch 咽喉点包住。MCP `is_mcp_tool` 旧旁路消失。

## 5. 改造阶段

### Phase 0 · 边界测试先行
- `tests/tools/test_tool_registry_boundary.py`：`ToolContext` 无 `agent`/`_session_mgr`/`lease` 属性；`UNTRUSTED` 工具未声明的能力槽为 `None`。
- `tests/tools/test_dispatch_authorization.py`：deny 的工具不进 `execute`；ask 经 broker 回路。
- `tests/tools/test_namespace_policy.py`：外部工具撞内置名 → fail-loud；override 仅 TRUSTED+显式。

### Phase 1 · `Tool` / `ToolRegistry` / `ToolContext` 骨架(内置先跑通)
- 新增 `tools/registry.py` 的 `Tool`/`ToolRegistry`/`Capability`/`Trust`/`ToolResult`/`ToolContext`。
- 内置工具改为带 `needs` 声明的 `Tool`；`tools/spec.py` 的 `_ALL` 改为「启动时 `register()`」。
- 删 `tools/registry.py` 旧 `tool_definitions` 与 `tools/execute.py` 的 `_HANDLERS`(改 `registry.schemas()` / `registry.get(name)`)。
- 暂保留 host-routed 工具走 router 分支(`needs` 声明已就位，行为下一阶段迁入 `execute`)。

### Phase 2 · ExecutionEnv 注入 + 能力把手铸造
- runtime 层(`RuntimeServices`)注入沙箱中介的 `ExecCap`/`FsReadCap`/`FsWriteCap`/`SpawnCap` 提供者。
- dispatch 按 `needs ∩ policy(trust)` 铸造把手进 `ToolContext`。
- 内置 fs 工具改走 `ctx.fs_*`(补 docs 记分卡 #4：文件操作纳入沙箱策略)。

### Phase 3 · host-routed 全自包含
- agent/skill/shell/memory/tasks/plan 改为 `execute(inp, ctx)`，行为只经 `ctx` 能力(`ctx.exec`/`ctx.spawn`/`ctx.memory`/`ctx.tasks`/`ctx.set_mode`)。
- 清空 `CapabilityRouter` 的 host-routed 分支(只剩授权 + 铸造 + 派发)。

### Phase 4 · 开放注册：MCP + 扩展 + 嵌入者
- MCP 工具注册成 `Tool(source=mcp:…, trust=UNTRUSTED, namespace)`；删 `is_mcp_tool` 旁路。
- 解禁 `extensions/api.py` 的 `register_tool`(经 dispatch 咽喉点包装，兑现 api.py:47 承诺)。
- `AgentConfig.tools` + `runtime.register_tool` 注入口。

### Phase 5 · 信任档 + namespace 策略收口
- `register()` 强制：外部强制 namespace、撞保留名 fail-loud、override 仅 TRUSTED+显式。
- 策略表 `policy_allows(capability, trust)`：`UNTRUSTED` 默认能力集 + 审批门。

## 6. 落地顺序建议

1. **Commit 1**：Phase 0 边界测试 + Phase 1 骨架(内置注册表跑通，删两个派生全局)。
2. **Commit 2**：Phase 2 ExecutionEnv 注入 + 能力把手铸造(内置 fs 纳沙箱)。
3. **Commit 3**：Phase 3 host-routed 全自包含 + 清空 router 分支。
4. **Commit 4**：Phase 4 开放注册(MCP 并表 + 扩展 register_tool + 嵌入者 slot)。
5. **Commit 5**：Phase 5 信任档 + namespace 策略 + 审批回路收口。

每个 Commit 全量测试绿；Commit 1 力争零行为变更(纯结构)。

## 7. 风险与处理

- **host-routed 迁移触面大**：Phase 3 逐工具迁，迁一个删一条 router 分支，保持每步可测。
- **权限不可下推**：能力把手是沙箱中介的，但「能不能调这工具」仍由②咽喉点裁决；`ctx.ask` 只在已授权范围内补确认，绝不替代咽喉点。
- **namespace 展平给模型**：模型只见展平名(`mcp__server__tool`)；内部按 `ToolName` 解析(Codex 式)，防撞 + 留 provenance。
- **审批回路**：复用 `RuntimeApprovalBroker`(异步 request/response)，使 RPC/TUI/SDK 一致接入。

## 8. 验收清单

### 8.1 边界(最高优先)
```text
- ToolContext 实例无 agent / _session_mgr / lease 可达路径。
- UNTRUSTED 工具未声明 / 未放行的能力槽为 None。
- 外部工具无法 shadow 内置名(register fail-loud)。
```

### 8.2 dispatch / 权限
- deny 工具不进 execute；ask 经 broker 回 current thread。
- exec/fs 把手强制 sandbox policy(writable_roots / network / engine)。

### 8.3 注册路径
- 内置 / MCP / 扩展 / 嵌入者四路均经 `register()` + 同一 dispatch；`is_mcp_tool` 旁路已删。
- `extensions/api.py` `register_tool` 可用且经权限包装。

### 8.4 不变量(神圣)
- PermissionEngine 裁决在②咽喉点(不下推进工具)。
- SessionLease 单写者不变(`ctx.spawn` 起的子 agent 经 runtime 注入 lease)。
- SandboxManager 是 exec/fs 把手的唯一中介。

## 9. 一句话准则

工具可以来自任何地方(内置/MCP/扩展/嵌入者)，但它的 `execute` 只吃 `ToolContext` 这个密封能力面、
按「声明 ∩ 信任档策略」铸造、且必先过 dispatch 咽喉点授权；`ToolContext` 里没有任何字段通向 raw
内核——于是「可嵌入」与「不破边界」同时成立。
