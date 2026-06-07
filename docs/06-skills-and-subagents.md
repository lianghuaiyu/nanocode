# 技能与子 Agent

> 源码位置：`src/nanocode/skills/` 与 `src/nanocode/subagents/`
> 关键文件：`skills/discovery.py`、`skills/resolve.py`、`subagents/config.py`、`subagents/prompts.py`

技能与子 Agent 是 nanocode 的两种扩展机制。技能是可复用的提示词模板（可内联注入或 fork 子 Agent 执行）；子 Agent 是带独立系统提示词与工具集的隔离 Agent，以 fork-return 模式跑完一个子任务并回传结果。两者都从 `.claude/` 目录发现，支持用户级与项目级。

## 它解决什么

随着任务复杂化，需要两类能力：把常用工作流封装成可一键调用的"技能"（如提交、生成问候），以及把探索、规划这类自包含的子任务交给一个聚焦的"子 Agent"独立完成，避免污染主对话上下文。两者都应可由用户在项目里声明、无需改源码。

## 它如何工作

### 技能

**发现**（`discovery.py`）：扫描 `~/.claude/skills/*/SKILL.md`（用户级）与 `./.claude/skills/*/SKILL.md`（项目级，覆盖同名用户级）。每个 `SKILL.md` 的 frontmatter 声明 `name` / `description` / `when_to_use` / `allowed-tools` / `user-invocable` / `context`，正文是提示词模板，解析为 `SkillDefinition`。结果有缓存（`reset_skill_cache()` 清空）。`context` 取 `inline`（默认）或 `fork`。

**解析与执行**（`resolve.py`）：`resolve_skill_prompt(skill, args)` 把模板中的 `$ARGUMENTS` / `${ARGUMENTS}` 替换为传入参数，把 `${CLAUDE_SKILL_DIR}` 替换为技能目录路径。`execute_skill(name, args)` 返回 `{prompt, allowed_tools, context}`。`build_skill_descriptions()` 生成系统提示词里的技能清单（区分"用户可调用 /<name>"与"自动可调用"）。

**两种执行模式**（在 `agent.engine` 中）：
- **inline**：解析后的提示词直接作为对话内容注入主 Agent，技能逻辑在当前上下文里执行。
- **fork**：构造一个子 Agent（系统提示词为技能正文，工具集为 `allowed-tools` 或默认排除 `agent`），独立 `run_once` 执行后把输出回传，技能的中间过程不进入主对话。

REPL 中用户输入 `/<skill> [args]` 即可调用用户可调用的技能；模型也可主动用 `skill` 工具调用。

### 子 Agent

**类型解析**（`config.py`）：`get_sub_agent_config(agent_type)` 返回该类型的 `{system_prompt, tools}`。内置三类：
- `explore` — 只读工具（`read_file` / `list_files` / `grep_search`），用于快速代码检索（`EXPLORE_PROMPT`）。
- `plan` — 只读工具，输出结构化实现计划（`PLAN_PROMPT`）。
- `general` — 除 `agent` 外的全部工具，用于独立完成任务（`GENERAL_PROMPT`）。

**自定义类型**：`.claude/agents/<name>.md`（用户级与项目级）经 frontmatter 声明 `name` / `description` / `allowed-tools`，正文为系统提示词。自定义类型与内置同名时覆盖内置。`get_available_agent_types()` 汇总所有类型供系统提示词列出，`build_agent_descriptions()` 仅在存在自定义类型时追加描述。

**fork-return 执行**（在 `agent.engine._execute_agent_tool`）：模型用 `agent` 工具发起，指定 `type` / `description` / `prompt`。engine 构造一个 `is_sub_agent=True` 的新 `Agent`（注入该类型的系统提示词与工具集，权限模式继承 plan 或设为 bypass），`run_once(prompt)` 跑完后回传文本与本次 token 增量，父 Agent 把结果作为工具结果纳入主对话。子 Agent 不自动保存会话、输出被缓冲整体返回，因此其内部多轮工具调用不会进入主上下文。

## 关键数据流 / 取舍

```
技能:
  发现 .claude/skills/*/SKILL.md → SkillDefinition（含 context）
  /<skill> args 或 skill 工具:
     inline → resolve_skill_prompt → 注入主对话
     fork   → 构造子 Agent(系统提示=技能正文) → run_once → 回传

子 Agent:
  agent(type, prompt):
     get_sub_agent_config(type) → {system_prompt, tools}
     新 Agent(is_sub_agent) → run_once → {text, tokens} → 作为工具结果回传
```

取舍：技能与子 Agent 都靠 `.claude/` 目录声明，无需改源码即可扩展；fork/子 Agent 在同进程协程内运行，隔离来自独立的系统提示词与工具白名单，而非操作系统沙箱；子 Agent 的 fork-return 把子任务的中间上下文与主对话隔离，代价是子 Agent 看不到主对话历史。
