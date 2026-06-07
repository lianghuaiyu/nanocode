# 上下文压缩

> 源码位置：`src/nanocode/agent/compaction.py` 与两个后端文件
> 关键文件：`compaction.py`（常量 + 大结果落盘）、`anthropic_backend.py` / `openai_backend.py`（分层裁剪与摘要压缩）

上下文窗口有限，而 coding agent 会快速积累大量工具结果。压缩系统用多层策略在不同占用程度下渐进地回收上下文：先裁剪、再剔除陈旧结果、再清理空闲历史、最后才做摘要式重建；同时把超大单条结果落盘，以预览 + 路径替换正文。

## 它解决什么

文件内容、grep 结果、shell 输出会迅速填满上下文。一刀切的"超限就摘要"会丢失太多信息且代价高。压缩系统希望：在轻度占用时无感、在中度占用时优先牺牲最不重要的内容（陈旧/重复的大结果）、只有在确实接近上限时才做有损的整体摘要，并对单条巨大结果用"落盘 + 可取回预览"避免一次性挤占上下文。

## 它如何工作

每轮 API 调用前，Agent 调用 `_run_compression_pipeline()`，按后端跑三层就地裁剪；接近窗口上限时，在轮次边界另行触发摘要式 auto-compact。落盘则发生在每条工具结果产出之后。

**大结果落盘**（`compaction.persist_large_result`）：当单条工具结果超过 30KB，把完整内容写入 `~/.nanocode/tool-results/`（路径经 `paths.tool_results_dir()` 解析，可由 `NANOCODE_HOME` 覆盖），上下文里只保留"大小/行数 + 文件路径 + 前 200 行预览"。模型若需要完整内容，可用 `read_file` 读回落盘文件，信息不丢失。

**分层就地裁剪**（两个后端各有一套，逻辑对称）：
- **Tier 1 — budget 截断**（`_budget_tool_results_*`）：当上下文占用 ≥ 50% 时，对超过预算（占用 >70% 时 15KB，否则 30KB）的工具结果做"保留首尾、中间省略"的截断。
- **Tier 2 — 剔除陈旧结果**（`_snip_stale_results_*`）：占用 ≥ `SNIP_THRESHOLD`（0.60）时，把可裁剪工具（`read_file` / `grep_search` / `list_files` / `run_shell`）的旧结果替换为占位符 `[Content snipped - re-read if needed]`，保留最近 `KEEP_RECENT_RESULTS`（3）条；对同一文件的重复 `read_file` 结果，只保留最后一次。
- **Tier 3 — microcompact**（`_microcompact_*`）：当距上次 API 调用空闲超过 `MICROCOMPACT_IDLE_S`（5 分钟）时，把除最近 3 条以外的工具结果清成 `[Old result cleared]`，回收长时间挂起会话的上下文。

**摘要式 auto-compact**（`_compact_anthropic` / `_compact_openai`）：`_check_and_compact` 在最近一次输入 token 超过有效窗口的 85% 时触发。它在轮次边界（最后一条是普通用户文本，而非 tool_result）调用模型，把"目前为止的对话"压成一段摘要，用"摘要 + 一句确认"重建消息历史，并把最后的用户消息接回。这是有损操作，因此特意只在轮次边界进行，避免割裂 tool_use ↔ tool_result 配对导致 API 报错。`/compact` 命令可手动触发同一流程。

## 关键数据流 / 取舍

```
每轮调用前: run_compression_pipeline
   占用 ≥50% → budget 截断
   占用 ≥60% → 剔除陈旧/重复结果（保留最近 3 条）
   空闲 ≥5min → microcompact 清旧结果

每条结果产出后: >30KB → 落盘 + 预览替换（可 read_file 取回）

输入 token > 85% 窗口（轮次边界）→ 摘要式 auto-compact 重建历史
```

取舍：阈值（50%/60%/85%、30KB、保留 3 条、5 分钟）均为经验值，目标是够用而非最优；前三层是无损或可取回的裁剪，只有摘要式 compact 是有损的，故放在最后且限定在轮次边界；落盘把"完整信息"移到磁盘，用一次可选的 `read_file` 换取上下文空间。
