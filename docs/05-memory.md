# 记忆系统

> 源码位置：`src/nanocode/memory/`
> 关键文件：`service.py`（host-owned 记忆边界）、`markdown_backend.py`（Markdown 后端）、`simplemem_backend.py` / `engines/simplemem/`（nanocode-owned SimpleMem 引擎）、`prompts.py`（按后端生成静态提示）、`recall.py`（召回注入模型）

记忆系统让 nanocode 把跨会话有价值的信息持久化为带类型的 Markdown 文件，并在恰当的用户输入到来时，由模型语义选取相关记忆注入当前上下文。它把"长期知识"与"当前对话"解耦：记忆按需召回，而非全部塞进每次请求。

## 它解决什么

有些信息值得跨会话保留：用户偏好、踩过的坑、项目约定、参考资料。但把所有记忆都注入每次请求既昂贵又会稀释注意力。记忆系统需要：用结构化方式存储记忆、维护一个可读索引、并在每个用户输入到来时只挑出"明显有用"的少数几条注入——而且不能阻塞主循环。

## 它如何工作

**存储**（`store.py`）：记忆是带 frontmatter 的 `.md` 文件，存放在按项目隔离的目录（`paths.project_memory_dir()`，对 cwd 做哈希，受 `NANOCODE_HOME` 控制）。每条记忆有四种类型之一：`user` / `feedback` / `project` / `reference`（`VALID_TYPES`）。`save_memory` / `list_memories` / `delete_memory` 提供 CRUD，文件名形如 `<type>_<slug>.md`。每次增删都会调用 `_update_memory_index()` 重写 `MEMORY.md` 索引（每条一行：名称、文件、类型、描述）。`load_memory_index()` 读取索引并对过长内容做行数/字节截断。

**静态提示注入**（`service.py` → `prompts.py`）：`MemoryService.static_prompt()` 按当前后端生成提示。Markdown 后端会把 `MEMORY.md` 索引拼进系统提示词；SimpleMem 后端只说明 indexed memory 和 `memory` 工具用法，不暴露文件路径。

**语义召回**（`recall.py`）：正文的召回是按需、语义化的：
- `scan_memory_headers()` 只读每个记忆文件的前 30 行 frontmatter，快速得到轻量头部（描述、类型、mtime），按修改时间倒序，上限 `MAX_MEMORY_FILES`（200）。
- `select_relevant_memories(query, side_query, already_surfaced)` 把候选记忆的清单（文件名 + 时间 + 描述）连同用户查询发给模型（`SELECT_MEMORIES_PROMPT`），要求返回一个 `selected_memories` 文件名数组（最多 5 条，且只选"明确有用"的）。被选中的记忆读入正文（超过 `MAX_MEMORY_BYTES_PER_FILE` 截断），并附上新鲜度信息。
- `format_memories_for_injection()` 把选中的记忆包进 `<system-reminder>` 块，作为 user 消息内容注入。

**新鲜度**：`memory_age` 给出"today / yesterday / N days ago"；`memory_freshness_warning` 对超过 1 天的记忆附加提醒——记忆是某一时刻的观察，可能已过时，断言前应对照当前代码核实。

**异步预取与门控**（`start_memory_prefetch`）：召回需要一次额外的模型调用，因此被设计成非阻塞的预取。`start_memory_prefetch` 在用户输入到来时创建一个后台 asyncio 任务并立即返回句柄；Agent 循环在任务 settled 后以零等待方式轮询取结果，再注入到最近的 user 消息。预取前有多重门控：输入必须多词（含空白）、本会话累计注入字节未超 `MAX_SESSION_MEMORY_BYTES`（60KB）、且记忆目录确有记忆文件。`already_surfaced` 集合避免同一记忆在一个会话内被重复注入。

## 关键数据流 / 取舍

```
保存: save_memory → 写 <type>_<slug>.md → 重建 MEMORY.md 索引
启动: RuntimeServices → MemoryService.static_prompt → backend-aware memory prompt

用户输入到来:
  门控（多词 / 预算未满 / 有记忆） → start_memory_prefetch（后台任务）
    scan_memory_headers → 清单
    side_query(模型) → selected_memories（≤5）
    读正文 + 新鲜度 → RelevantMemory
  循环中 settled? → 注入 <system-reminder> 到最近 user 消息 → 记入 already_surfaced
```

取舍：召回靠一次额外模型调用换取精准度，因此用预取 + 门控把延迟与开销摊到非关键路径；记忆是点时观察而非实时状态，故显式附加新鲜度警告；记忆按项目（cwd 哈希）隔离，避免不同项目的记忆互相污染。
