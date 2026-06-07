# 权限系统

> 源码位置：`src/nanocode/tools/permissions.py`
> 关键文件：`permissions.py`（判定逻辑）、`run_shell.py`（危险命令检测）

权限系统在每次工具执行前做出三态判定：`allow` / `deny` / `confirm`。它把"模式 + 声明式规则 + 危险命令检测"组合成一个纯函数 `check_permission`，让 Agent 循环据此决定直接执行、拒绝、还是请用户确认。

## 它解决什么

让模型自主执行命令与改文件是有风险的。权限系统需要在"足够自动化以可用"和"足够保守以安全"之间给出可配置的平衡：交互场景下危险操作要确认，CI 场景下要自动拒绝，受信场景下要全部放行，规划场景下要只读。同时要让用户能用简单规则白/黑名单具体操作。

## 它如何工作

**5 种权限模式**：
- `default` — 只读工具直接放行；危险 shell 与"写/编辑不存在的文件"需 `confirm`；其余放行。
- `plan` — 只读放行；`write_file`/`edit_file` 仅允许写计划文件，其余编辑与所有 `run_shell` 一律 `deny`。
- `acceptEdits` — 在 default 基础上，文件编辑自动放行，危险 shell 仍需确认。
- `bypassPermissions` — 全部放行（`--yolo`）。
- `dontAsk` — 凡是本该 `confirm` 的操作改为 `deny`（适合 CI）。

**判定顺序**（`check_permission`）：
1. `bypassPermissions` → 立即 `allow`。
2. 声明式规则：deny 规则命中 → `deny`；allow 规则命中 → `allow`（deny 优先于 allow）。
3. 只读工具（`read_file` / `list_files` / `grep_search` / `web_fetch`）→ `allow`。
4. `plan` 模式：编辑非计划文件 → `deny`；`run_shell` → `deny`。
5. `enter/exit_plan_mode` 元工具 → `allow`。
6. `acceptEdits` 模式下的编辑工具 → `allow`。
7. 危险判定：危险 shell 命令、写/编辑不存在的文件 → 需确认；此时 `dontAsk` 改判 `deny`，否则返回 `confirm`。
8. 兜底 → `allow`。

**声明式规则**（`.claude/settings.json`）：从用户级（`~/.claude/settings.json`）与项目级（`./.claude/settings.json`）读取 `permissions.allow` / `permissions.deny`。每条规则经 `_parse_rule` 解析：`run_shell(git push *)` → `{tool: "run_shell", pattern: "git push *"}`，纯工具名 `read_file` → `{tool: "read_file", pattern: None}`。匹配时（`_matches_rule`）：`run_shell` 比对 `command`，其余比对 `file_path`；模式以 `*` 结尾按前缀匹配，否则精确匹配；无模式则只要工具名相同即命中。规则结果有缓存（`_cached_rules`），`reset_permission_cache()` 清空。

**危险命令检测**（`run_shell.py` 的 `is_dangerous` / `DANGEROUS_PATTERNS`）：用一组正则识别诸如 `sudo`、`rm` 删除、`git push`、重定向覆盖等高风险命令。命中即触发确认流。这是基于正则的启发式，不解析命令 AST。

**确认流**：Agent 循环在收到 `confirm` 时调用注入的 `confirm_fn`（REPL 中是终端 y/n 输入）。已确认过的具体操作会记入 `_confirmed_paths`，同一会话内不再重复询问。

## 关键数据流 / 取舍

```
工具调用 X(inp), mode, plan_file_path
  → bypass? allow
  → 规则 deny/allow?
  → 只读工具? allow
  → plan 模式编辑/shell? deny（计划文件除外）
  → acceptEdits 编辑? allow
  → 危险（危险 shell / 写新文件）? 
        dontAsk → deny
        否则    → confirm → confirm_fn → allow / deny
  → allow
```

取舍：检测与规则匹配基于正则/前缀，实现简单、可读，但相比命令 AST 分析存在被绕过的可能；权限是一个纯函数，便于单测覆盖各模式与边界；规则来源限定为用户级与项目级两层 `settings.json`，project 优先级更高。
