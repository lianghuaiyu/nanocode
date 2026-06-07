# Repository Guidelines

## Project Structure & Module Organization
核心源码位于 `src/nanocode/`（src-layout）。入口在 `src/nanocode/entrypoints/cli.py`（`nanocode` 命令）与 `src/nanocode/__main__.py`（`python -m nanocode`）。功能按域分目录：
- `agent/` — Agent 主循环：`engine.py`（编排）、`anthropic_backend.py` / `openai_backend.py`（双后端）、`compaction.py`（上下文压缩）、`plan_mode.py`、`models.py`（模型元数据/重试）。
- `tools/` — 工具系统：一工具一模块（`read_file.py` 等），加 `registry.py`（工具表）、`execute.py`（分发）、`permissions.py`、`shared.py`。
- `memory/`、`skills/`、`subagents/`、`mcp/`、`session/` — 记忆、技能、子 Agent、MCP 集成、会话持久化。
- 跨切面核心置于包顶层：`paths.py`、`prompt.py`、`system_prompt.md`、`ui.py`、`frontmatter.py`。
测试在顶层 `tests/`，镜像包结构。架构文档在 `docs/`。示例在 `examples/`。

## Build, Test, and Development Commands
- `pip install -e ".[dev]"`：可编辑安装并装上 pytest。
- `nanocode`：启动交互式 REPL。`nanocode --help`：查看用法与 flags。
- `pytest -q`：运行测试套件。
- 本地存储默认在 `~/.nanocode`，测试用 `NANOCODE_HOME` 环境变量隔离。

## Coding Style & Naming Conventions
Python ≥ 3.11，4 空格缩进。`snake_case` 命名函数/变量，`PascalCase` 命名类。模块小而专一：每个工具一个模块，跨域导入用包级 `__init__` 再导出。新增工具时，在 `tools/<name>.py` 导出 `SCHEMA`（具体工具另导出 `run`），并在 `tools/registry.py` 登记。

## Testing Guidelines
测试放在 `tests/`，目录镜像包结构（如 `tests/tools/test_permissions.py`）。用 `tmp_path` 与 `NANOCODE_HOME` 隔离文件系统，不发网络请求。新增行为请补对应单测；命名 `test_<行为>`。

## Commit & Pull Request Guidelines
用简短祈使句提交信息（如 `Fix MCP config normalization`），可加类型前缀（`refactor:` / `test:` / `docs:`）。PR 说明用户可见影响、关键改动与验证步骤。
