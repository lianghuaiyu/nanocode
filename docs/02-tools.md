# 工具系统

> 源码位置：`src/nanocode/tools/`
> 关键文件：`registry.py`、`execute.py`、`shared.py`，以及一工具一模块的 `read_file.py` / `write_file.py` / `edit_file.py` / `list_files.py` / `grep_search.py` / `run_shell.py` / `sandbox_shell.py` / `web_fetch.py` / `memory_tool.py` / `tasks_tool.py`

工具系统定义了模型能做什么。每个工具是一个独立模块，导出一份 schema 与（对具体 I/O 工具而言）一个 `run` 函数；注册表把所有 schema 聚合成工具表，分发器在运行时按名调用对应处理函数，并施加读写防护与结果截断。

## 它解决什么

模型要操作真实环境，就需要一组定义清晰、行为可预期的工具。工具系统需要回答三个问题：模型看到哪些工具（schema）、调用时如何执行（分发）、如何防止危险或无效操作（防护）。nanocode 用"一工具一模块 + 注册表 + 分发器"的结构，让新增/修改单个工具不牵动其他部分。

## 它如何工作

**SCHEMA / run 契约**：每个具体工具模块导出 `SCHEMA`（Anthropic 工具 schema 的 dict，含 `name` / `description` / `input_schema`）和 `run(inp: dict) -> str`（同步处理函数）。元工具（`skill` / `agent` / `plan` / `tool_search`）只导出 schema，其执行分别落在 `agent.engine`（skill/agent/plan）与 `tools.execute`（tool_search）。

**注册表**（`registry.py`）：导入所有工具模块，把它们的 `SCHEMA` 拼成 `tool_definitions` 列表。它还维护 deferred（延迟加载）工具的状态：
- `get_active_tool_definitions(tools)` 返回当前生效的工具 schema，会剔除尚未激活的 deferred 工具，并去掉 `deferred` 键（该键不应发给 API）。
- `get_deferred_tool_names(tools)` 返回尚未激活的 deferred 工具名，供系统提示词提示模型"可通过 tool_search 取回"。
- `_activated_tools` 集合记录已激活的 deferred 工具，`reset_activated_tools()` 清空（测试隔离用）。

**分发**（`execute.py`）：`async execute_tool(name, inp, read_file_state)` 是统一入口（异步协程，便于在 Agent 主循环中并发编排 IO），用 `_HANDLERS` 字典把工具名映射到各模块的 `run`。它在分发前后施加三类逻辑：

1. **read-before-edit + mtime 防护**：`read_file` 成功后，把文件的绝对路径与当前 mtime 记入 `read_file_state`。`write_file` / `edit_file` 执行前检查：若目标文件已存在但未被读过，拒绝并提示"必须先 read_file"；若读过但 mtime 与记录不符（外部被改动），警告并要求重新读取。写入成功后更新 mtime 记录。这避免模型基于陈旧或未知内容覆盖文件。
2. **tool_search 激活**：`tool_search` 在 deferred 工具中按查询词匹配名称/描述，把命中项加入 `_activated_tools` 并返回它们的 schema，从而把这些工具"展开"给后续轮次。
3. **结果截断**：所有工具结果经 `shared._truncate_result` 截断到 `MAX_RESULT_CHARS`，防止单次结果撑爆上下文（更大的结果会在 Agent 层进一步落盘）。

**共享辅助**（`shared.py`）：`_normalize_quotes`（花引号归一，提升 edit 容错）、`_find_actual_string`（在文件中定位待替换串，兼容引号差异）、`_generate_diff`（生成 `@@` 形式的 diff 供 edit 输出）、`MAX_RESULT_CHARS` / `_truncate_result`。

**包对外接口**（`tools/__init__.py`）：仅做再导出，外部统一从 `nanocode.tools` 取 `tool_definitions` / `execute_tool` / `check_permission` / `is_dangerous` 等，内部模块如何拆分对调用方透明。

## 关键数据流 / 取舍

```
模型请求工具 X(inp)
  → check_permission（见 03-permissions）
  → execute_tool(X, inp, read_file_state)
      read_file?      → run + 记录 mtime
      write/edit?     → 校验已读 & mtime 新鲜 → run → 更新 mtime
      tool_search?    → 激活 deferred 工具并返回 schema
      其他            → _HANDLERS[X](inp)
  → _truncate_result
  → （Agent 层）大结果落盘 → tool_result
```

取舍：工具均为同步实现，IO 并发由 Agent 层在协程中编排，而非每个工具自带异步；deferred 工具机制以"减少初始上下文中的工具数量"为目的，按需展开；防护层只覆盖文件读写一致性，命令安全交由权限层（正则）处理。新增工具的成本很低：写一个 `tools/<name>.py` 导出 `SCHEMA` 与 `run`，在 `registry.py` 与 `execute._HANDLERS` 登记即可。
