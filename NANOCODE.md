# nanocode

nanocode 是一个从零构建的命令行 coding agent（Python）。本文件是 nanocode 在自身仓库内运行时加载的项目说明（nanocode 会读取 `NANOCODE.md` 与 `AGENTS.md`）。nanocode 运行时会从当前目录的 `.nanocode/` 与用户主目录的 `~/.nanocode/`（`NANOCODE_HOME`）读取用户提供的 skills / agents / rules 与权限配置（`settings.json`）。

详细仓库结构、构建/测试命令与约定见同目录的 `AGENTS.md`。
