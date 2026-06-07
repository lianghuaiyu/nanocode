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

- **双后端**：Anthropic 原生 Messages API 与任意 OpenAI 兼容端点，统一编排，按环境变量自动选择。
- **流式 + 早期工具执行**：逐字流式输出；只读工具在流式响应尚未结束时即并行启动，缩短端到端延迟。
- **5 模式权限 + 声明式规则**：`default` / `plan` / `acceptEdits` / `bypassPermissions` / `dontAsk` 五种模式，叠加 `.claude/settings.json` 的 allow/deny 规则与危险命令正则检测。
- **多层上下文压缩 + 大结果落盘**：按上下文占用分级触发 budget 截断 → 陈旧结果裁剪 → microcompact → 摘要式 auto-compact；单次工具结果超过 30KB 自动写盘并以预览 + 路径替换，模型可按需 `read_file` 取回。
- **4 类型记忆 + 语义召回**：`user` / `feedback` / `project` / `reference` 四类记忆，`MEMORY.md` 索引，按用户输入异步预取并由模型语义选取相关记忆注入上下文。
- **技能（inline / fork）**：从 `.claude/skills/*/SKILL.md` 发现技能，支持内联注入与 fork 子 Agent 两种执行模式，解析 `$ARGUMENTS` 与 `${CLAUDE_SKILL_DIR}`。
- **子 Agent（fork-return）**：内置 `explore` / `plan` / `general` 三类，外加 `.claude/agents/` 自定义类型；以 fork-return 模式独立运行并回传结果与 token 统计。
- **MCP 集成（stdio）**：通过 JSON-RPC over stdio 连接外部工具服务器，自动发现工具并以 `mcp__server__tool` 前缀路由调用。
- **Plan Mode**：只读规划模式，把计划写入计划文件，经四选项审批流后再切换到执行模式。
- **预算控制**：`--max-cost`（美元）与 `--max-turns`（轮次）双维度限额，超限自动停止。
- **会话持久化**：每轮对话自动保存，`--resume` 恢复上次会话。
- **跨平台**：Windows / macOS / Linux，自动检测 shell。

## 架构

源码采用 src-layout，核心包在 `src/nanocode/`，按域分目录：

```
src/nanocode/
├── entrypoints/
│   └── cli.py              # CLI 参数解析、REPL、信号处理
├── agent/
│   ├── engine.py           # Agent 编排：主循环、工具分发、子 Agent、预算
│   ├── anthropic_backend.py# Anthropic 流式对话与分层压缩
│   ├── openai_backend.py   # OpenAI 兼容流式对话与分层压缩
│   ├── compaction.py       # 压缩常量 + 大结果落盘
│   ├── plan_mode.py        # Plan Mode 进入/退出/审批
│   └── models.py           # 模型元数据、重试、工具格式转换
├── tools/
│   ├── registry.py         # 工具表 + deferred 工具激活
│   ├── execute.py          # 调用分发 + read-before-edit 防护
│   ├── permissions.py      # 5 模式权限 + 声明式规则
│   ├── shared.py           # 共享辅助（diff、截断、引号容错）
│   ├── read_file.py …      # 一工具一模块（read/write/edit/list/grep/shell/web_fetch）
│   └── skill.py / agent.py / plan.py / tool_search.py  # 元工具 schema
├── memory/                 # 记忆：store（CRUD+索引）/ recall（语义召回）/ prompt_section
├── skills/                 # 技能：discovery（发现）/ resolve（解析执行）
├── subagents/              # 子 Agent：config（类型解析）/ prompts（内置提示词）
├── mcp/                    # MCP：connection（单连接）/ manager（多服务器路由）
├── session/                # 会话持久化
├── paths.py                # 集中本地存储路径（NANOCODE_HOME）
├── prompt.py               # System Prompt 构建（@include、模板替换）
├── system_prompt.md        # 外置系统提示词模板
├── ui.py                   # 终端输出
└── frontmatter.py          # 共享 YAML frontmatter 解析
```

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
| `agent/engine.py` | 主循环编排、工具调用分发、子 Agent/技能执行、预算检查 |
| `agent/*_backend.py` | 两种 API 形态的流式对话与分层压缩实现 |
| `tools/` | 工具 SCHEMA/run 契约、注册表、分发、权限、read-before-edit 防护 |
| `memory/` | 四类型记忆 CRUD、索引、语义召回与异步预取 |
| `skills/` | 技能发现与 inline/fork 执行 |
| `subagents/` | 内置与自定义子 Agent 类型解析 |
| `mcp/` | MCP stdio 连接、工具发现与前缀路由 |
| `session/` | 会话保存/恢复/列举 |
| `prompt.py` | System Prompt 构建（模板替换、@include、上下文采集） |

详尽的子系统说明见 [`docs/`](./docs/) 下的 8 篇架构专题。

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
| `/compact` | 手动触发对话压缩 |
| `/memory` | 列出已保存的记忆 |
| `/skills` | 列出可用技能 |
| `/<skill>` | 调用已注册技能（如 `/commit`） |

## 配置与扩展点

nanocode 在当前目录与用户主目录读取 `.claude/` 配置（项目级优先于用户级）：

- **权限规则** — `.claude/settings.json` 的 `permissions.allow` / `permissions.deny`，规则形如 `run_shell(git push *)` 或 `read_file`。
- **技能** — `.claude/skills/<name>/SKILL.md`，frontmatter 声明 `name` / `description` / `context`（inline 或 fork）/ `allowed-tools` 等，正文为提示词模板。
- **子 Agent** — `.claude/agents/<name>.md`，frontmatter 声明 `name` / `description` / `allowed-tools`，正文为该子 Agent 的系统提示词。
- **规则** — `.claude/rules/*.md` 会被自动并入系统提示词。

MCP 服务器通过 `.mcp.json`（或 `.claude/settings.json` 的 `mcpServers`）配置。仓库自带一个可运行示例 `examples/mcp_echo_server.py`：

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
- **权限基于正则**：危险命令检测与权限规则是正则/前缀匹配，而非命令 AST 解析，可能存在绕过。
- **压缩策略为简化实现**：分层压缩的阈值与摘要提示词是经验值，目标是够用而非最优。
- **记忆召回依赖一次额外模型调用**：语义选取通过 sideQuery 完成，在低延迟场景会增加开销。
- **费用估算为粗略口径**：成本按固定单价估算，仅用于预算门控，不等同于账单。

这些取舍都集中在少数文件里，便于按需替换或加固。

## License

MIT，详见 [LICENSE](./LICENSE)。
