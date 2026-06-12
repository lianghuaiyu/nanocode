<div align="center">

# nanocode

**一个从零构建的命令行 coding agent，约 4000 行 Python。**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](./LICENSE)

</div>

---

nanocode 是一个真实可用的命令行编程智能体：它把消息历史送入大模型，让模型自主调用工具读写代码、执行命令、检索信息，并在多轮迭代中完成开发任务。整个 Agent 栈——Agent 循环、工具系统、权限控制、上下文压缩、记忆、技能、子 Agent、MCP——都从第一性原理用 Python 实现，没有黑盒。

## 设计理念

nanocode 的目标是**完整掌握一个真实 coding agent 的每一层**。每个子系统都是可读、可改、可测的最小实现：

- 不依赖框架封装的"agent 抽象"，主循环就是一段能看懂的 `while` 循环。
- 工具、权限、压缩、记忆等都是独立的域，可以单独阅读、单独替换。
- 双后端（Anthropic 原生 + OpenAI 兼容）共享同一套编排逻辑，便于对比两种 API 形态。

它不是玩具：流式输出、并行工具执行、多层上下文压缩、语义记忆召回、MCP 集成等生产级特性都已落地，同时把实现规模控制在可以一人通读的体量。

## 特性

- **分层可嵌入 runtime（docs/15）**：`SessionManager` 是唯一 durable truth，`AgentCore.state` 只是可丢弃投影；`AgentCore`（模型循环）/ `AgentSession`（state↔树同步）/ `ContextRuntime`（请求时上下文工程）/ `CapabilityRouter`（工具单一 dispatch）/ `runtime.spawn`（子 agent 编排）分层清晰，`RuntimeThread.events()` 暴露 in-process 事件流供 SDK/协议消费者驱动。
- **请求时上下文工程 + repo map**：项目指令、memory、git、skill listing、repo map 等都是结构化 `ContextPack`（带 lifecycle / cache 策略 / 预算 / compaction 存活），由 `ContextRuntime` 按预算组装、经 `/context` 可审计；稳定 system 前缀利于 prompt cache。Aider-style 词法 `RepoIndex` 把代码库结构作为 ContextProvider 注入（个性化排名 + 预算封顶）。
- **双后端**：Anthropic 原生 Messages API 与任意 OpenAI 兼容端点，统一编排，按环境变量自动选择。
- **流式 + 早期工具执行**：逐字流式输出；只读工具在流式响应尚未结束时即并行启动，缩短端到端延迟。
- **5 模式权限 + 声明式规则**：`default` / `plan` / `acceptEdits` / `bypassPermissions` / `dontAsk` 五种模式，叠加 `.nanocode/settings.json` 的 allow/deny 规则与危险命令正则检测。
- **多层上下文压缩 + 大结果落盘**：按上下文占用分级触发 budget 截断 → 陈旧结果裁剪 → microcompact → 摘要式 auto-compact；单次工具结果超过 30KB 自动写盘并以预览 + 路径替换，模型可按需 `read_file` 取回。
- **4 类型记忆 + 语义召回**：`user` / `feedback` / `project` / `reference` 四类记忆，`MEMORY.md` 索引，按用户输入异步预取并由模型语义选取相关记忆注入上下文。
- **技能（inline / fork）**：从 `.nanocode/skills/*/SKILL.md` 发现技能，支持内联注入与 fork 子 Agent 两种执行模式，解析 `$ARGUMENTS` 与 `${CLAUDE_SKILL_DIR}`。
- **子 Agent（隔离 · 强制 · 可审计的 runtime）**：内置 `explore` / `plan` / `general`，外加 `.nanocode/agents/`（与 vendor-neutral 的 `.agents/agents/`）自定义类型。manifest frontmatter 可声明 `tools` / `disallowed-tools` / `extends`（仅能收窄）/ `model` / `max-turns` / `timeout-ms`。子 Agent **上下文隔离**、前后台均**有界**（max-turns + wall-clock 超时）、工具权限在**调用期强制**（声明只能收窄不能放大，`agent` 工具与 fork 一律禁止——子不 spawn 后代），结果以**结构化 AgentResult + 有界信封**回传（完整产物落盘，模型按需 `read_file` 取回）。`/agents` 管理可用定义与运行实例。
- **MCP 集成（stdio）**：通过 JSON-RPC over stdio 连接外部工具服务器，自动发现工具并以 `mcp__server__tool` 前缀路由调用。
- **Plan Mode**：只读规划模式，把计划写入计划文件，经四选项审批流后再切换到执行模式。
- **预算控制**：`--max-cost`（美元）与 `--max-turns`（轮次）双维度限额，超限自动停止。
- **会话 = canonical session.jsonl 树**：每条消息以干净原文写进 `~/.nanocode/sessions/<id>/session.jsonl`（Pi-style 时间有序 tree，唯一 durable truth）；上下文由 `build_context()`（fold + render）从树重建，注入是 render-time 的 `custom_message` entry、不污染原始消息。支持 in-file 分支（`/fork` `/checkout` `/tree`）、跨文件 `/clone`、子 session 导航（`/agent` `/parent`）。子 Agent 写独立 child session.jsonl，父分支只存有界结果信封 + child session id。
- **跨平台**：Windows / macOS / Linux，自动检测 shell。

## 架构

源码采用 src-layout，核心包在 `src/nanocode/`，按域分目录：

```
src/nanocode/
├── entrypoints/
│   └── cli.py              # CLI 参数解析、REPL、信号处理（runtime client）
├── agent/                  # Agent Core（L2）：运行态投影，非 durable truth
│   ├── core.py             # AgentCore：模型循环 + 流式消费 + 工具调度 + 事件发射
│   ├── loop.py             # provider-independent 循环辅助（OpenAI 批处理分组等）
│   ├── providers.py        # ProviderAdapter：Anthropic/OpenAI 流式 + capture 归一
│   ├── state.py            # AgentState/ProviderProjection：build_context() 的可丢弃投影
│   ├── events.py           # typed AgentEvent union
│   ├── session.py          # AgentSession：state ↔ canonical 树同步边界
│   ├── runtime.py          # AgentRuntime / RuntimeThread（含 events() 订阅）
│   ├── engine.py           # Agent：bootstrap + collaborators 宿主（职责持续迁出、收缩中）
│   └── compaction.py / plan_mode.py / models.py / subagent_manager.py
├── session/                # canonical session.jsonl 树（L3，唯一 durable truth）
│   ├── tree.py             # Entry envelope + 中立 Message 构造器 + 纯函数
│   ├── manager.py          # 树存储 + flock 单写者 + build_context
│   ├── context.py          # fold：branch → rich messages + 标量 + compaction 两区折叠
│   ├── render.py           # 中立 Message → provider 合法 payload
│   ├── capture.py          # provider 输出 → 中立 facts
│   └── lease.py            # 写者租约（writer identity 归 runtime active thread）
├── context/                # 请求时上下文工程（L1，Claude Code-style）
│   ├── runtime.py          # ContextRuntime：按预算 + 缓存策略组装 ContextPlan
│   ├── providers.py        # ContextProvider：项目指令/git/memory/skill/repo-map…
│   └── packs.py / ledger.py / budgets.py / cache_policy.py
├── codeintel/              # 代码库结构感知（Aider-style）
│   ├── index.py            # RepoIndex：扫描 + 词法 symbol 抽取 + 个性化排名 + 预算渲染
│   └── symbols.py          # SymbolTag + 语言探测
├── agents/                 # AgentProfile（subagent / 多 agent 基础）
│   └── profile.py / registry.py / permissions.py / result.py
├── capabilities/           # 工具/MCP/skill/subagent 单一 dispatch
│   ├── router.py           # CapabilityRouter：allowlist 咽喉点 + meta/agent/skill/real 路由 + hooks
│   └── permissions.py      # 不可变 PermissionContext
├── runtime/                # 子 session / 多 agent 编排层（L4）
│   ├── spawn.py            # SubAgentRunner：子 agent 构造 / 前后台执行 / 产物落盘
│   └── teams.py            # TeamRuntime 骨架（多 agent 协作预留）
├── tools/                  # 一工具一模块 + registry / execute / permissions
├── memory/ skills/ subagents/ mcp/ tasks/ trajectory/   # 记忆 / 技能 / 子 agent 配置 / MCP / 后台任务 / 轨迹
├── paths.py                # 集中本地存储路径（NANOCODE_HOME）
├── prompt.py               # 稳定 system prompt 构建（@include、模板替换）
├── system_prompt.md        # 外置系统提示词模板
├── ui.py                   # 终端输出
└── frontmatter.py          # 共享 YAML frontmatter 解析
```

> **分层（docs/15）**：`session/`=唯一 durable truth；`agent/state.py`=可丢弃运行态投影；`agent/core.py`=模型循环；`agent/session.py`=两者的同步边界；`context/`=请求时上下文工程；`codeintel/`=代码库结构感知；`capabilities/`=工具单一 dispatch；`runtime/`=子 session/多 agent 编排。一句话：**Pi 管可信历史与状态重建，Claude Code 管请求时上下文工程，Aider 管代码库结构感知**。详见 [`docs/15-agent-core-context-runtime-multiagent-refactor.md`](./docs/15-agent-core-context-runtime-multiagent-refactor.md) 与落地路线图 [`docs/15-IMPL-roadmap.md`](./docs/15-IMPL-roadmap.md)。

Agent loop 的核心数据流：

```
用户输入
  │
  ▼
┌─────────────────────────────────────┐
│           Agent Loop                │
│                                     │
│  消息历史 → API（流式）→ 实时输出   │
│       ▲                   │         │
│       │              ┌────┴───┐     │
│       │              │文本输出│     │
│       │              │工具调用│     │
│       │              └────┬───┘     │
│       │                   │         │
│       │   ┌───────┐ ┌────▼───┐     │
│       │   │结果落盘│←│工具执行│     │
│       │   └───────┘ └────┬───┘     │
│       │                   │         │
│       │   ┌───────────────▼───┐     │
│       └───│ Token 追踪 + 压缩 │     │
│           └───────────────────┘     │
└─────────────────────────────────────┘
  │
  ▼
任务完成 → 自动保存会话
```

模块职责一览：

| 模块 | 职责 |
|------|------|
| `agent/core.py` · `loop.py` · `providers.py` | AgentCore 模型循环 + provider-independent 辅助 + ProviderAdapter（流式/capture 归一） |
| `agent/state.py` · `events.py` · `session.py` | AgentState 可丢弃投影 / typed AgentEvent / AgentSession（state↔树同步、turn-end 一致性校验） |
| `agent/engine.py` | Agent bootstrap + collaborators 宿主（职责持续迁出新分层、收缩中） |
| `agent/runtime.py` | AgentRuntime / RuntimeThread 句柄 + `events()` in-process 事件订阅 |
| `session/` | canonical session.jsonl 树（唯一 durable truth）：tree/manager/context(fold)/render/capture/lease |
| `context/` | 请求时上下文工程：ContextRuntime + ContextProvider + Pack/Ledger/Budget/cache 策略 |
| `codeintel/` | Aider-style 代码库结构感知：词法 RepoIndex + RepoMapProvider + SymbolTag |
| `agents/` | typed AgentProfile + registry（发现/extends 收窄/信任闸）+ child≤parent 权限派生 + ResultEnvelope |
| `capabilities/` | CapabilityRouter 工具单一 dispatch（allowlist 咽喉点 + 路由 + hooks）+ 不可变 PermissionContext |
| `runtime/` | 子 session / 多 agent 编排：SubAgentRunner（spawn）+ TeamRuntime 骨架 |
| `tools/` | 工具 SCHEMA/run 契约、注册表、分发、权限、read-before-edit + read_file 预算封顶 |
| `memory/` · `skills/` · `subagents/` · `mcp/` | 记忆（四类型 + 语义召回）/ 技能（inline·fork）/ 子 agent 配置 / MCP stdio 路由 |
| `prompt.py` | 稳定 system prompt 构建（项目指令/memory 改由 ContextRuntime 注入，不再烤进 system） |

详尽的子系统说明见 [`docs/`](./docs/) 下的架构专题（01–15）；docs/15 是 Agent Core + Context Runtime + 多 Agent 分层改造报告，配套落地路线图见 `docs/15-IMPL-roadmap.md`，逐次改动记录见 [`docs/devlog/`](./docs/devlog/)。

## 快速开始

需要 Python 3.11+。

```bash
pip install -e .
```

### 配置 API

支持两种后端，通过环境变量自动识别（均支持自定义 base URL）：

**方式一：Anthropic 格式**

```bash
export ANTHROPIC_API_KEY="sk-ant-xxx"
export ANTHROPIC_BASE_URL="https://api.anthropic.com"   # 可选，可指向兼容代理
```

**方式二：OpenAI 兼容格式**

```bash
export OPENAI_API_KEY="sk-xxx"
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

以上环境变量也可写进**当前目录**的 `.env` 文件，nanocode 启动时自动加载（已 `export` 的变量优先，`.env` 不覆盖）。可复制 `.env.example` 作为模板，`.env` 会被 Git 忽略。

默认模型为 `claude-opus-4-6`，可用环境变量或命令行覆盖：

```bash
export NANOCODE_MODEL="claude-sonnet-4-6"   # 环境变量
nanocode --model gpt-4o                     # 命令行（优先级更高）
```

### 运行

```bash
nanocode                 # 交互式 REPL
nanocode "fix the bug in src/app.py"   # 一次性任务
```

## CLI 用法

```
nanocode [options] [prompt]

  --yolo, -y          跳过所有确认（bypassPermissions 模式）
  --plan              Plan 模式：只读，先规划再执行
  --accept-edits      自动批准文件编辑，危险 shell 仍需确认
  --dont-ask          需确认的操作一律自动拒绝（适合 CI）
  --thinking          启用扩展思考（仅 Anthropic）
  --model, -m         指定模型（默认 claude-opus-4-6，或 NANOCODE_MODEL）
  --api-base URL      使用 OpenAI 兼容端点
  --resume            恢复上次会话
  --max-cost USD      估算费用超过此值即停止
  --max-turns N       超过 N 轮即停止
  --help, -h          显示帮助
```

## REPL 命令

| 命令 | 功能 |
|------|------|
| `/clear` | 清空对话历史 |
| `/plan` | 切换 Plan 模式（只读 ↔ 正常） |
| `/cost` | 显示 token 用量与费用估算 |
| `/context` | 显示当前上下文 packs、token 预算与 compaction 存活（ContextLedger） |
| `/compact` | 手动触发对话压缩 |
| `/memory` | 列出已保存的记忆 |
| `/skills` | 列出可用技能 |
| `/agents` | 列出可用子 Agent 定义与运行中实例（`available` / `running` / `show <name\|id>`） |
| `/<skill>` | 调用已注册技能（如 `/commit`） |

## 配置与扩展点

nanocode 在当前目录与用户主目录读取 `.nanocode/` 配置（项目级优先于用户级；用户级根可用 `NANOCODE_HOME` 覆盖，默认 `~/.nanocode`）：

- **权限规则** — `.nanocode/settings.json` 的 `permissions.allow` / `permissions.deny`，规则形如 `run_shell(git push *)` 或 `read_file`。
- **子 Agent 并发/限额** — `.nanocode/settings.json` 的 `agents` 段：`max_threads`（并发后台子 Agent 上限）/ `max_depth`（spawn 深度上限）/ `default_timeout_ms` / `background_timeout_ms`。
- **技能** — `.nanocode/skills/<name>/SKILL.md`，frontmatter 声明 `name` / `description` / `context`（inline 或 fork）/ `allowed-tools` 等，正文为提示词模板。
- **子 Agent** — `.nanocode/agents/<name>.md`（或 vendor-neutral 的 `.agents/agents/<name>.md`），frontmatter 声明 `name` / `description` / `tools`（= `allowed-tools`）/ `disallowed-tools` / `extends` / `model` / `max-turns` / `timeout-ms`，正文为该子 Agent 的系统提示词。**工具声明只能收窄父权限、不能放大，且在调用期强制**；**项目级子 Agent 定义需工作区受信任才加载**（非交互/未信任运行不会静默加载项目本地 agent 定义）。
- **规则** — `.nanocode/rules/*.md` 会被自动并入系统提示词。

> 兼容性说明：早期版本读取 `.claude/`；当前代码已迁移到 `.nanocode/`（信任存储与会话产物在 `~/.nanocode/` 下，绝不写入项目目录）。

MCP 服务器通过 `.mcp.json`（或 `.nanocode/settings.json` 的 `mcpServers`）配置。仓库自带一个可运行示例 `examples/mcp_echo_server.py`：

```json
{
  "mcpServers": {
    "echo": {
      "command": "python",
      "args": ["examples/mcp_echo_server.py"]
    }
  }
}
```

## 运行测试

```bash
pip install -e ".[dev]"
pytest
```

测试套件用 `NANOCODE_HOME` 与 `tmp_path` 隔离本地存储，不发起网络请求。

## 设计取舍与已知局限

nanocode 追求"可通读"，因此在若干维度做了刻意简化：

- **单进程**：工具与子 Agent 都在同一进程内以 asyncio 协程运行，没有进程级沙箱隔离。需要隔离时可用 `sandbox_shell`：在隔离的 microVM 中执行命令（opt-in，需 `msb`），用于不可信命令/装包/隔离测试；`run_shell` 仍是默认执行后端。
- **子 Agent 隔离是逻辑层而非文件系统层**：子 Agent 的边界靠调用期工具 allowlist、权限继承（只收窄）、深度/并发上限、项目信任闸来保证；尚无 per-子-Agent 的 git worktree / 文件系统隔离（写型 fork 与父共享工作树）。
- **权限基于正则**：危险命令检测与权限规则是正则/前缀匹配，而非命令 AST 解析，可能存在绕过。
- **压缩策略为简化实现**：分层压缩的阈值与摘要提示词是经验值，目标是够用而非最优。
- **记忆召回依赖一次额外模型调用**：语义选取通过 sideQuery 完成，在低延迟场景会增加开销。
- **费用估算为粗略口径**：成本按固定单价估算，仅用于预算门控，不等同于账单。

这些取舍都集中在少数文件里，便于按需替换或加固。

## License

MIT，详见 [LICENSE](./LICENSE)。
