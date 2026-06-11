# docs/13 — Pi-style 会话树迁移（Canonical JSONL Session Tree）· v2

状态：**权威设计 v2（已经一轮对抗式评审 + Pi coding-agent 源码核验加固）**。取代 v1。
定位：L3（事件溯源会话运行时）。
**定调（已锁）**：nanocode 现状**不是权威**。本设计**完整移植 Pi 应用层（`packages/coding-agent`）的会话模型并主动精简**——canonical `session.jsonl` 树是**唯一可重建事实源**；为换取干净模型，**主动舍弃** nanocode 的 snip/microcompact 压缩管线、`messages.json` 快照、wire-as-authority、双 provider `MessageStore`。

> v2 的每条"Pi 怎么做"都引用了 **`packages/coding-agent`（应用层，nanocode 的真正对照）** 或 `packages/agent`（核心 harness）的真实 `文件:行`。源码基线 commit **`9ccfcd7cfcacdf593c0b24929d1d847e6cdf6711`**（本地 clone 与之逐字一致）。

> **统一原则（贯穿全文）：存全事实，render 严格 gate。** 存储层忠实记录真实 assistant content——含 thinking 块 + `thinkingSignature`/`redacted`、`stopReason`、甚至 aborted turn；**能不能安全发回 provider 全由 render（`transformMessages + 各 provider convertMessages`）按 `isSameModel` + 合法性判定**。drop-aborted、孤儿补 tool_result、tool_result 合并、thinking 重放/降级，都是这一原则的实例。

---

## 0. v1 → v2 改了什么（决策与精简）

**锁定的决策**
1. **源真相**：canonical `session.jsonl` 树**唯一权威**。删 `messages.json` 快照、删 wire 双轨。trajectory/export 从 `session.jsonl` **单向派生**。
2. **压缩**：**删除 snip/microcompact**（nanocode 独有、Pi 没有、且依赖 wall-clock+token 制造不可重建态）。唯一缩上下文手段 = **summary-compaction-as-entry**（Pi 模型，摘要进树、可重建、可 fork）。
3. **注入**（skill listing / memory recall / finished-task reminder）：做成 **`custom_message` entry**（Pi 应用层就是这么干），fold 进上下文；**不再原地改 user 消息**。
4. **消息表示**：单一**中立 Message**模型 + send 时 `render`；**收编**双 provider `MessageStore`。
5. **faithfulness**：abort 的 turn 走 **render 层 drop-and-retry**（Pi 行为，靠 `stopReason`），**放弃** nanocode 现有的"快照保半成品"。换来：无快照、纯确定性 render。**thinking 块亦存全**（含 `thinkingSignature`/`redacted`），由 render 按 `isSameModel` 严格 gate（§4⑦）——即"存全事实、render 严格 gate"。
6. **leaf**：日志 entry；无 state.json 权威（顶多派生 cache）。
7. **设置 entry 拆分**、**子会话 = child session**、**`/fork` 默认 in-file**：维持 v1；但 child-session 设计吸收 OpenCode 的优点：父子 session 一等索引、可恢复、可导航、可查询，而不是回到 Pi extension 的 `--no-session` 子进程。**子会话链接落在 `agent` tool_call/tool_result 上（无独立 off-path entry，§7.2），`task_id == childSessionId`。**

**主动舍弃的 nanocode 功能（换干净模型）**
- snip/microcompact 增量裁剪（→ 改用 summary compaction）。
- `messages.json` / flat session JSON 作为 resume 权威（→ 仅迁移期只读源）。
- `wire.jsonl` 作为独立事实源 + durable 契约 guard（→ trajectory 改从 `session.jsonl` 派生；wire 退役或降纯 debug trace）。
- 双 provider `MessageStore`（→ 单中立模型 + render）。
- abort turn 的半成品保留（→ Pi drop-and-retry）。

**评审加固（已核验并入）**：render 双移植（transform + per-provider convertMessages）、补 `convertToLlm` 中间层、补 `custom_message`/`custom`/`active_tools_change` entry、捕获 `stopReason`、system prompt 不入树、修 fork 不复制史/save-point/leaf-null/crash-safe 等误述、子会话单写者强制（advisory lock）、Pi 字段名（`modelId`、branch_summary `details`、compaction 无 `coveredEntryIds`）。

---

## 1. 目标

```text
session   = append-only JSONL entry tree（可周期性 _rewriteFile GC，非教条 append-only）
entry.id + entry.parentId = 会话树（parentId 语义父，行序≠树）
activeLeaf = 当前位置（日志 entry，加载时折叠重建）
context   = getBranch(leaf) → fold 为中立 Message[] → render(provider)（纯确定性）
```

效果：一个 session 目录即完整事实源；resume 只读 `session.jsonl`；fork 廉价；compaction/注入/tool/model 变更皆一等 entry；trajectory 从树单向派生。

**非目标（不动）**：RuntimeEvent/command 架构、权限系统、subagent caps/governance。

---

## 2. Pi 应用层事实基线（packages/coding-agent，带 file:行）

> coding-agent 自带一个**完整 `SessionManager`**（`session-manager.ts`，1567 行，docstring 749-755：「entry 有 id/parentId 构成树，leaf 指针追踪当前位置，append 创建当前 leaf 的子，branching 把 leaf 移到更早 entry」）——这就是 nanocode 要写的那个类的**直接参照**。

1. **无破坏性压缩**：Pi 在**工具产出时**就 cap 所有大输出（read `truncateHead` 50KB/2000 行、find、grep，**不止** bash；`bash-executor.ts:124` 等），故无 in-place snip/microcompact。缩上下文唯一手段 = summary-compaction，**存为 `compaction` entry**（`{summary, firstKeptEntryId, tokensBefore}`，`compaction/compaction.ts:104-105`），fold 时变 `createCompactionSummaryMessage`（`compaction.ts:90`），**positional** 从 `firstKeptEntryId` 重建（`prepareCompaction` `compaction.ts:644-666`）；原 entry 不删、可 fork。
   > ⚠️ **据此修正 §0.2 的删 snip 前提**：必须先为 `read_file`（`read_file.py` 当前**无 cap**、整文件读入——nanocode 真正缺保护的面）移植 Pi 式 per-read byte/line cap（可复用 nanocode 已有的 `persist_large_result` 30KB→disk），**否则删掉 snip 后单次大 read 会在 summary-compaction 触发前撑爆窗口**。这是评审纠正的事实错误（我此前误称 Pi 仅 cap bash）。
2. **注入 = `custom_message` entry**：extension 产出的 `role==="custom"` 消息 → `appendCustomMessageEntry`（`agent-session.ts:506-515`）；普通 user/assistant/toolResult → `appendMessage`（:516-523）；compactionSummary/branchSummary「在别处持久化」（:524）。
3. **`stopReason` 一等字段**：`assistantMsg.stopReason !== "error"` 驱动 auto-compaction/retry（`agent-session.ts:531`）；render 据它丢 aborted turn（`packages/ai/.../transform-messages.ts:186-194`）。
4. **leaf 无 sidecar**：`private leafId`（内存）+ 持久进 jsonl；**无 state.json、无 messages.json**；resume = `loadEntriesFromFile(sessionFile)`（`session-manager.ts:466/795`）只读 jsonl。
5. **三段管线**：`getBranch` → `buildSessionContext` 产 `AgentMessage[]`（含 compaction/branchSummary/custom 合成）→ `convertToLlm` 降为中立 `Message[]`（`compaction.ts:12/583`，messages.ts:120-164）→ provider adapter（transform-messages + 各 provider `convertMessages`）。
6. **非教条 append-only**：`_rewriteFile()` 整文件重写（`session-manager.ts:872`，803/812/1350 调）——可用于 GC/迁移。
7. **子会话 = 独立 `--no-session` 子进程**（`packages/coding-agent/examples/extensions/subagent/index.ts:288`）——nanocode 的 child-session 是**主动超越**。

---

## 3. Canonical 数据模型（规范）

### 3.1 envelope

```jsonc
{
  "v": 1,
  "id": "ent_01J...",      // uuidv7 式、时间有序、与 agent_id/seq 解绑
  "parentId": "ent_...",   // 语义父；root 为 null
  "sessionId": "sess_...",
  "type": "message",
  "timestamp": "2026-...Z",
  "data": { }
}
```
- **`turnId`/`branchId` 从 envelope 移除**（评审 m11：Pi `SessionTreeEntryBase` 只有 `{type,id,parentId,timestamp}`）。turn 归组**下游派生**（按 user-message 边界）；branch 身份就是 leaf/parentId，不再要独立 `branchId`。trajectory 若需链接，从树结构派生。
- **id（Python 落地，评审 m8）**：`requires-python>=3.11`,3.11–3.13 **无 stdlib `uuid.uuid7()`**（3.14+ 才有）。用 `uuid6` backport 或手写 monotonic 生成器（**进程安全**:共享 worktree 多进程）。用**全长**时间有序 id（非 Pi 的 8 字符截断 + 撞 id 重试——截断重试是 Pi 因为截断才需要的,`jsonl-storage.ts:35-41`）。

### 3.2 entry union

| type | 类别 | fold? | data（Pi 字段名） |
|---|---|---|---|
| `session_start` | 头/根 | 否 | 第一行,`parentId=null`,兼 cheap-listing header(`version/id/cwd/parentSession/timestamp`；`createdAt` 为 timestamp 派生、不入存储——Pi `jsonl-storage.ts:8-15/116`) |
| `message` | 消息族 | 是 | `{message: 中立 Message}`(user/assistant/toolResult,见 §4.1) |
| `custom_message` | 消息族(合成) | 是 | `{customType, content, display, details?}` — **注入/合成消息的家**(Pi `agent-session.ts:506-515`) |
| `custom` | 扩展数据 | 否 | `{customType, data?}` |
| `compaction` | 派生上下文 | 是 | `{summary, firstKeptEntryId, tokensBefore, details?, fromHook?}`(**无** `coveredEntryIds/tokensAfter`,评审 m4) |
| `branch_summary` | 派生上下文 | 是 | `{fromId, summary, details?, fromHook?}`(readFiles/modifiedFiles 在 `details` 下,评审 m3) |
| `model_change` | 设置(标量 fold) | 否 | `{provider, modelId}`(评审 m5) |
| `thinking_level_change` | 设置 | 否 | `{thinkingLevel}` |
| `active_tools_change` | 设置 | 否 | `{activeToolNames}`(评审 M10:fold 进 activeTools 标量,subagent 工具门控审计要) |
| `leaf` | 导航 | 否 | `{targetId: str\|null}`(**null=重置到 root→空上下文**,评审 m10) |
| `label` | 元数据 | 否 | `{targetId, label}` LWW,空=tombstone |
| `session_info` | 元数据 | 否 | `{name?}` LWW,**末个胜出、空则清空**(评审 m6) |
| `permission_decision` | 运行时事实 | 否 | nanocode 特有,审计/trajectory |
| `task_update` | 运行时事实 | 否 | nanocode 特有 |
| ~~`agent_spawn` / `agent_result`~~ | — | — | **不再独立 entry**（§7.2 优化）：子会话链接 = `agent` tool_call + bounded toolResult（`details.childSessionId`）；后台完成 = 合成 `custom_message`。仅 `task_update` 记后台状态跃迁 |
| `session_end` | 生命周期 | 否 | — |

> **system prompt 不是 entry**(评审 M5):它是 render 时的 Context(Pi `Context.systemPrompt`),OpenAI 的 in-list system 消息在迁移时剥离、render 时重建;plan-mode 后缀作 render 侧输入。

### 3.3 中立 Message（抄 Pi `packages/ai/src/types.ts:235-313`）

- `UserMessage{role:"user", content: str | (Text|Image)[]}`
- `AssistantMessage{role:"assistant", content:(Text|Thinking|ToolCall)[], provider, api, model, stopReason, usage?, timestamp}` — **`stopReason` 必存**(评审 B2)
- `ToolResultMessage{role:"toolResult", toolCallId, toolName, content:(Text|Image)[], details?, isError}` — **`details?` 必须保留**（Pi `types.ts:308`；§7 子会话链接的 `{childSessionId, taskId, status, summary, resultPath, files, tokens, error}` 即存于此）
- block:`Text{text, textSignature?}` / `Thinking{thinking, thinkingSignature?, redacted?}` / `Image{data, mimeType}` / `ToolCall{id, name, arguments(已解析对象), thoughtSignature?}`
- **`ToolCall.arguments` 规范化**(评审 m1):OpenAI 后端存的是**原始 JSON 串**,Anthropic 存 dict;捕获/迁移时统一解析为对象。
- **thinking（已定调，照 Pi：存全 + render gate）**：**默认把 thinking 当一等内容块存全**——含 `thinkingSignature` 与 `redacted`（Anthropic：`thinking` block → `{thinking:"", thinkingSignature:""}` 流式累加 `signature_delta`；`redacted_thinking` → `{redacted:true, opaque 入 thinkingSignature}`，`anthropic.ts:550-568/621-627`；OpenAI-compat：reasoning 字段进 `ThinkingContent`，`thinkingSignature` 存 reasoning 字段名，`openai-completions.ts:217-223/329-337`）。`stopReason` **必捕获**。存储层**不取舍**——能否发回交给 §4 render gate。

---

## 4. 上下文管线（规范，三段，纯确定性）

```text
getBranch(leaf) → 沿 parentId leaf→root,反转 root-first(O(depth))

fold(branch) → AgentMessage[]:                 # 评审 M1:这一段不是"纯拼接"
  scalar 折叠(LWW): model(从 model_change AND assistant 消息记录的 provider/model,二者取末,评审 M8)
                    / thinkingLevel / activeToolNames / 记最后一个 compaction
  若有 compaction C:                            # 评审 m12:两区
    push compactionSummary(C.summary)
    区一 [0, C 索引): 仅 firstKeptEntryId 起的 entry
    区二 (C 索引, 末]: 全收
  收 message / custom_message / branch_summary(非空) 为 AgentMessage

convertToLlm(AgentMessage[]) → Message[]:        # 评审 M1/M2:降为中立 + 包前缀
  compactionSummary/branchSummary → 中立 user 消息(带 PREFIX/SUFFIX 包装);custom_message → 中立 user 消息(content **原样、无 PREFIX**,messages.ts:148-195——否则会改写注入的 `<system-reminder>` 文本)

render(Message[], model) → provider payload:     # 评审 B2/B3:transform + convertMessages 双移植
  ① 孤儿 tool_call 合成 error tool_result(transform-messages.ts:160-177)
  ② 丢 stopReason∈{error,aborted} 的 assistant turn（transform-messages.ts:186-194 仅 `continue` 跳过该 assistant）；**并删其被孤立的 tool_result（inverse-orphan）——这是 nanocode 新增的 render 逻辑，Pi 不做**：Pi 的 aborted turn 是空失败消息、不带 tool_use，丢弃不产生孤儿；nanocode 的 abort 可能**已写入** tool_result，丢掉 assistant 后会留下 toolCallId 无对应 tool_use 的 toolResult（Anthropic 拒收）。故 render 必须显式删除"其 toolCallId 在存活 tool_call 中无匹配"的 toolResult；**不得**把此行为归于 `:186-194`
  ③ Anthropic:多个 tool_result 并进一条 user 消息(anthropic.ts:1113-1146);空块/空 assistant 丢弃
     OpenAI:tool role + requiresAssistantAfterToolResult 桥接;system 单独
  ④ tool-call id 归一(Anthropic 64 字符 ^[A-Za-z0-9_-]$;OpenAI 40 字符 pipe-split,评审 B3)
  ⑤ 不支持 image 的 model:image→placeholder
  ⑥ system prompt(+plan-mode 后缀)作 render 侧输入注入
  ⑦ thinking gate(`isSameModel` = provider/api/model 全等,transform-messages.ts:90-113):
     同模型 → 保留带签名 thinking + redacted thinking(原样转回 `redacted_thinking`/`{thinking,signature}`,anthropic.ts:1066-1097);
     跨模型 → 丢 redacted、非空 thinking 降级 text(transform-messages.ts:101-113)、删 ToolCall.`thoughtSignature`(:128-131);
     无签名 thinking 默认降级 text,除非 `model.compat.allowEmptySignature`(anthropic.ts:180)
```

**为什么无快照即可**:删掉 snip(无不可重建压缩态)+ 注入是 `custom_message` entry + compaction 摘要是 entry ⇒ `render(fold(getBranch(leaf)))` 是**(branch, model, system)→payload 的纯函数**,resume 逐字重建,无需 `messages.json`/`llm_request` 快照。**abort 的 turn 走 ②**(drop-and-retry,Pi 行为;放弃半成品保留)。

---

## 5. 目录布局

```text
~/.nanocode/sessions/<session_id>/
  session.jsonl          # canonical 唯一事实源(可 _rewriteFile GC)
  state.json             # 可选派生 cache(activeLeaf/title/updatedAt);可由 jsonl 重建;非权威
  artifacts/<...>
```
子会话:**扁平 `sessions/<child_id>/` + 父 `agent` toolResult `details.childSessionId` 指针 + child `session_start.parentSession` 回指**(非物理嵌套,避免生命周期耦合)。

---

## 6. 集成接缝（nanocode 文件）

| 文件/类 | 改动 |
|---|---|
| `session/tree.py`(新) | envelope+union+`leaf_id_after_entry`+`get_branch`+`current_leaf`+`labels_by_id`+`session_name`;纯函数 |
| `session/manager.py`(新) | `SessionManager`(对照 coding-agent `session-manager.ts`):`create/open`、`append(type,data,parent_id=None)`、`set_leaf`/`get_leaf`(显式 leaf-entry,**非** append 隐藏 flag)、`get_branch`、`build_context`、`fork`/`clone`、`children(parent_session_id)`/`parent(child_session_id)`/`siblings(child_session_id)`、`_rewrite_file`(GC) |
| `session/context.py`(新) | `fold`(→ AgentMessage[]) + `convert_to_llm`(→ 中立 Message[]) |
| `session/render.py`(新) | **transform + 各 provider convertMessages 双移植**(§4 render);各 backend 的 id 归一作 per-provider callback |
| `session/migration.py`(新) | §10 迁移 |
| `agent/engine.py` | **捕获 `stop_reason`/abort 写入 assistant entry**(评审 B2 前置);turn 写树;config 变更 emit `*_change`;消息 message-end **立即落树**(评审 M7),仅 config/leaf 排队到 turn 边界;`restore_session` 改走 `build_context`;subagent spawn 先 mint child session、父写 `agent` tool_call/bounded toolResult;后台完成由主写者注入合成 `custom_message`;**删 `_run_compression_pipeline`**(snip/microcompact)与 `_inject_*` 原地改写(→ `custom_message` entry) |
| `agent/anthropic_backend.py`/`openai_backend.py` | 入口从"读 live provider 列表"改为"接 `render()` 输出";**停止剥离 thinking,改为完整捕获 thinking 块 + `thinkingSignature`/`redacted`**(anthropic.ts:550-568/621-627;OpenAI reasoning 字段);删原地注入 |
| `agent/message_store.py` | **收编**:live 上下文中立化、payload send-time render;不再双 provider 列表 |
| `agent/session.py`(`AgentSession`) | `move_to`/`fork`/`clone`/`set_name`/`set_label`;fail-closed;leaf 动后 re-fold |
| `agent/context_builder.py` | 降薄/退役:`rebuild`→`SessionManager.build_context`;**退役 snapshot fallback** |
| `tasks/models.py`+`tasks/manager.py` | `TaskRecord/SubAgentRecord` 增 `child_session_id` 与 `legacy_agent_id`;`resume`/`task_output` 先按 child session 解析,再兼容旧 `agent-001` |
| `agent/runtime.py`+`entrypoints/*` | 落 `Control` 分支(cli.py:568 占位);命令见 §8;`--session/--fork/--clone` |
| `trajectory/project.py`+`metrics.py` | **重写**(评审 m2:两个独立消费者)从 `session.jsonl` 派生;退役 wire durable 契约 guard 或改钉 session schema |
| 既有 `AgentSession.fork_to`/`Tracer.begin_branch`/wire `branch_id` | **同一 PR 退役/替换**(评审 M4),避免两套 fork |

**热路径切换**:每个 turn 边界(及 resume/move_to/fork)`SessionManager.build_context()` 产中立 Message[];backend 渲染发送。turn 内消息即时落树;无双写、无快照。

---

## 7. 子会话（child session,Pi 主线 + OpenCode 启发）

Pi 给子 agent `--no-session`(§2.7)。nanocode 需要 background+resumable subagent,所以这里**主动超越 Pi**；但边界必须锁死：主会话仍是 Pi-style canonical `session.jsonl` entry tree，OpenCode 只提供 child-session 产品化闭环的参考，不引入 SQLite 表、HTTP server 依赖或"session DB 才是权威"。

> **OpenCode 对照基线**：`sst/opencode@826419127ae0c2b742b9db866c4a9afb27a5ae2c`。**只吸收 child-session 闭环**：task tool 创建 child session、`parent_id`/`children` 导航、bounded `tool_result`、background inject、permission derive、`task_id == childSessionId`；**不吸收** SQLite/HTTP/API 作为权威。

### 7.1 硬边界

- **父 session**：只保存 bounded link/status/result，不保存子 transcript，不把子消息 fold 进父上下文。
- **子 session**：是普通 nanocode session 目录，拥有自己的 `session.jsonl`、leaf、compaction、custom_message、artifacts、lock。
- **父子关系**：由两侧冗余索引保证可恢复、可导航：
  - 父 `agent` toolResult `details.childSessionId` 指向 child。
  - child `session_start.data.parentSession` 回指 `{sessionId, entryId, taskId?, agentId?, toolCallId?}`。
  - 派生索引 `children(parentSessionId)` 只扫 session headers / `session_start`，不是新权威表。

### 7.2 关系即工具调用（优化：取消独立 spawn/result entry，消解评审 B4）

**核验 OpenCode `tool/task.ts` 后的关键优化**：OpenCode **不另设** spawn/result 记录——子会话的"生成"就是父会话里那条 `task` **tool_call**（其 metadata 带 `{parentSessionId, sessionId(child), model, background}`，`task.ts:175-185`），"结果"就是对应的 **tool_result**（bounded `<task id=child state=...><summary>…</summary><task_result>…</task_result></task>` 信封，`renderOutput` `task.ts:64-79`，foreground 在 `:320-324` 返回）。

落到 nanocode canonical 树——**不再要 `agent_spawn`/`agent_result` 独立 off-path entry，也不要 `advances_leaf=False`**：

- **前台 subagent**：`agent` tool_call（assistant message 的 `ToolCall`，input 带 spawn 参数）+ bounded 信封 **toolResult message**（`ToolResultMessage.details` 带结构化 `{childSessionId, taskId(=childSessionId), agentType, status, summary, resultPath, files, tokens, error}`）——**都是主路径普通 `message` entry，正常推进 leaf**。完整 transcript 只在 child session。
- **后台 subagent**：tool_call + **立即** `state="running"` toolResult（合法闭合 tool round，父可继续）。child 完成时**权威结果在 child session**；父侧只追加一条 bounded 通知 `custom_message`，且**必须挂在 spawn 所在分支（spawn tool_call 的 entry）之下，而非完成时的 live leaf**——否则父若已 `/rewind`|`/fork` 到别的分支 L2，结果会落到从未 spawn 它的分支、且 `children()` 配对扫不到（评审命中：OpenCode **无**此 hazard，因其 session 是线性 thread + soft `revert`，`session/session.ts:73,816-830`，inject 永远落在唯一 spawn 它的 thread 末端；nanocode 是分支树，必须显式 pin）。
- **Idle 路径**：若 child 完成时父正 idle 等输入（无 turn 边界），background done-callback 须**入队一条 nextTurn 合成消息并打断 stdin 等待 / 在下个 REPL tick 排空**（对标 OpenCode `Runner.ensureRunning` 从 idle 起 run，`runner.ts:131-135`）；否则结果被 strand 到下次用户消息。**被动 `custom_message` 不会自动通知 idle 父**——撤回此前"auto-notify（task.ts:31-41）"的等同声明，那段在 OpenCode 是经 `inject()`→`prompt()`→`ensureRunning` 主动 re-prompt 实现的。

**这消解评审 B4**：link 是普通 tool_call/tool_result（前台）或一条合成 message（后台），都自然推进 leaf——无 off-path 非 leaf entry、无"不许动主 leaf"的魔法、无跨协程直写父文件。`children(parent)` 的**权威源是 child 侧 `session_start.parentSession` 回指扫描**（branch-independent，survives 父 abort/rewind/fork——含父 `agent` tool_call 因 abort 无 toolResult 的情形）；父 branch 里 `toolName=="agent"` toolResult `details.childSessionId` 仅作**加速 cache**，须与 header 回指对账（双向冗余、非权威表）。

> 仅保留 `task_update`（可选，运行时事实 entry）记录后台 child 的状态跃迁（running→completed/error/cancelled）；运行中/孤儿状态本也可由"child `session.jsonl` 无 `session_end` 且父无注入结果"派生。

### 7.3 Child `session_start` metadata

child 的第一行 `session_start` 扩展字段：

```jsonc
{
  "parentSession": {
    "sessionId": "sess_parent",
    "entryId": "ent_spawn",
    "taskId": "task-001",
    "agentId": "agent-001",
    "toolCallId": "toolu_..."
  },
  "agent": {
    "type": "explore",
    "source": ".nanocode/agents/explore.md",
    "modelId": "claude-...",
    "provider": "anthropic",
    "background": true
  },
  "trajectoryId": "traj_parent"
}
```

这等价于 OpenCode 的 `session.parent_id`，但落在 Pi-style `session.jsonl` header 中。`parentSession.sessionId + parentSession.entryId` 是父子导航的稳定锚点；`agentId` 只是兼容当前 `SubAgentRecord(id="agent-001")` 的显示/迁移字段。

### 7.4 单一标识 + resume / steer（优化：`task_id == childSessionId`）

OpenCode 把三个 id 合一:**`task_id` 就是 child `sessionId`,也是 background job id**——传旧 `task_id` 即 `sessions.get(SessionID.make(task_id))` 续同一子会话(`task.ts:47-50,121-123`；`task_id` 作为 background jobId 见 `task.ts:246-276`)。nanocode 对齐:

- **唯一权威标识 = `childSessionId`（== `taskId` == background `jobId`）**;`legacyAgentId`(`agent-001`)仅迁移期查表兼容,解析到 child session 后继续。
- 新 spawn 先 mint `childSessionId`,作 child 目录名 + 父 tool_call/tool_result 的 `details.childSessionId`。
- 恢复子 agent = open child `sessions/<childSessionId>/session.jsonl`、读其 branch,而非父 `agents/<agent-id>/messages.json`。
- "给运行中后台子 agent 追加上下文 / steer" → 写 **child** session 的 user/`custom_message`;父只在 child 完成时追加 bounded 结果(§7.2)。


### 7.5 权限继承（借鉴 OpenCode，但保持 nanocode governance）

OpenCode 在 spawn 时派生 child permission（`deriveSubagentSessionPermission` 定义在 `agent/subagent-permissions.ts:18-34`、调用点 `task.ts:128-132`：child = **父 deny 并集 + `external_directory` 继承**（注意是 deny-union，**非** allow 交集），并对未显式许可的 subagent 默认 deny `todowrite:*` / `task:*`（`childToolDenies` `task.ts:133-145`））。nanocode 对应规则：

- child permissions = parent effective permissions ∩ agent manifest permissions；deny 取并集。（**注：∩-allows 是 nanocode 自加的更严规则；OpenCode 本身只 deny-forward——deny 并集 + parent-AGENT 的 edit denies + `external_directory` 继承，无 allow 交集**，见 §7.2/§引用锚点。）
- background child 使用 auto-deny confirm_fn，且 confirmed paths 不回流父。
- 默认禁止 child 再调用 `agent`（保持现状防递归）；只有将来显式打开 `max_depth>1` 且 manifest 允许时才可放开。
- 派生后的 permission snapshot 写入 child `session_start.data.agent.permission` 或紧随其后的 `permission_decision`/`custom` entry，便于 resume 与 trajectory 重放。

### 7.6 查询 / 导航 / UI

最小 API 是 `SessionManager.children(parent_session_id)`、`parent(child_session_id)`、`siblings(child_session_id)`，全部从 session headers 派生，可建 cache 但非权威。

顶层 `/sessions` **隐藏 child session**（过滤 `parentSession != null`），子会话只在 `/agents` 下按父归组——OpenCode 把 child 当 sub-session、不混入顶层列表（`children(parentID)` 按 `parent_id` 过滤见 `session/session.ts:638-646`，`listByProject` 以 `roots`→`parent_id IS NULL` 隐藏 child 见 `:1022-1024`；child 还跳过标题自动生成 `session/prompt.ts:183`）。

CLI 行为：
- 父 session `/agents`：列出当前父会话的 child sessions，显示 `taskId / agentType / status / summary / childSessionId`。
- 父 session `/agent <id>`：进入 child session；`id` 可为 `childSessionId`、`taskId` 或 legacy `agentId`。
- child session `/parent`：回父 session 的 `parentSession.entryId` 附近。
- child session `/agent next|prev`：在同一父 session 的 siblings 间切换。

这吸收 OpenCode 的 Parent/Prev/Next 优点，但实现仍是 Pi-style file scan + `move_to`/session open，不引入 OpenCode TUI 数据层。

### 7.7 迁移现状模型

现状是同一 `session_id` 下的 `agents/<agent-id>/{messages,meta,prompt,result,wire}.jsonl?`。迁移到 child session 时：

- 每个 `SubAgentRecord` mint 一个 `childSessionId`，生成 `sessions/<childSessionId>/session.jsonl`。
- `agents/<agent-id>/messages.json` → child `message` entries；`prompt.txt` → child root user message 或 `custom_message` provenance；`result.md` → child artifact + 父 `agent` toolResult `details.resultPath`。
- 旧 `agentId` 写入 child `session_start.data.parentSession.agentId` 和 `SubAgentRecord.legacyAgentId`，仅做兼容查找。
- 旧 `task_id` 写入 `TaskRecord.child_session_id`；`task_output` 先展示 bounded summary，再给 child session 路径。

### 7.8 单写者强制

**child-session-id 铸造 + `trajectory_id` 显式串**(评审 B4):今天子 agent 共享父 `session_id`(engine.py:954),给独立 id 会让 `traj_<id>` 静默分叉——P6 须补铸造路径并显式串 `trajectory_id`，使父子可归同一 trajectory。

**单写者强制(评审 B4)**:共享 worktree 多进程 + 全仓零 flock ⇒ append-only 树会被交错 append 损坏。加**per-session advisory lock / state.json owner-PID + 陈旧检测**:第二个 resume 同 session 的进程**改为 fork 新 session**。每个 child session 有自己的 lock；父结果写入只能由父主写者 flush。

### 7.9 契约测试

- spawn 后父 branch 出现 `agent` tool_call + bounded toolResult（`details.childSessionId`），child `session_start.parentSession.sessionId == parent`。
- 前台 toolResult 正常推进父 leaf；后台合成 `custom_message` pin 到 **spawn 分支**并推进之（**无 off-path entry、无 `advances_leaf=False`**）。
- **rewind/fork-after-spawn-before-finish**：父在后台 child 完成前 `/rewind`|`/fork` 到分支 L2 后，结果仍落在 spawn 分支（不污染 L2），`children()` 仍能配对（靠 child header 回指，非 live-leaf 扫描）。
- **idle 完成**：父 idle 时 child 完成 → done-callback 打断 stdin/入队 nextTurn，结果被surface（不 strand 到下次用户消息）。
- `children(parent)` 能列出 child（**权威=child header 回指**，含父 abort 无 toolResult 的情形）；`siblings(child)` 排序稳定；`parent(child)` 回到原 spawn `parentSession.entryId`。
- **resume 多重性**：`resume=<childSessionId>` 复用同一 child（多个父 `agent` tool_call → 一个 child，`children()` 按 childSessionId 去重）；`resume=<legacy agentId>` 只做兼容映射。
- background 完成写 child session 终态；父结果作为合成 `custom_message` 由父主写者（idle 则经唤醒路径）注入。
- child permission 不高于父；background confirm auto-deny；默认 child 不能再 spawn agent。

---

## 8. CLI / UX

| 命令/参数 | 树操作 | 行为 |
|---|---|---|
| `/session` | 读 fold | 当前 session/leaf/branch/路径 |
| `/tree` | `move_to` | entry 树;选节点→`leaf`;可选 branch summary |
| `/rewind`(或 `/tree` 选 user msg) | `move_to(user.parentId)` | 跳到该 user 消息父、原文回填编辑器(`agent-harness.ts:817-838`) |
| `/fork [entry]` | in-file 分支 | 默认 in-file(不 mint 额外身份);`--new-session` 跨文件 |
| `/clone [entry]` | 新 session | 复制 path-to-root(Pi 跨文件 fork **会**复制史,评审 M6) |
| `/checkout <entry>` | `move_to` | 切 leaf 不新分支 |
| `/sessions` | 列表 | header-only 扫描,按 cwd 分组、`parentSession` 血缘 |
| `/agents` | `children(current)` | 父 session 列 child sessions；child session 显示 siblings/parent |
| `/agent <id>` | open child | `id` 可为 `childSessionId`/`taskId`/legacy `agentId`;打开 child session |
| `/agent next\|prev` | sibling open | child session 内在同父 siblings 间切换 |
| `/parent` | open parent | child session 内回父 session,并定位到 `parentSession.entryId` |
| `--session <id>/latest`、`--fork/--clone <sid>:<entry>` | open/fork | 命令行直入 |

> **fork vs in-file 澄清(评审 M6)**:in-file 分支(`move_to`)**不复制**;跨文件 `/clone` **复制** path-to-root。

---

## 9. 分阶段计划

**P0 — 冻结 + 本文档。** wire 标 legacy;新能力只进 `SessionManager`;加 schema 契约测试。

**P1 — `SessionManager` + 中立 schema + render 双移植。** `session/{tree,manager,context,render}.py`;leaf 进日志;`build_context`(fold+convert_to_llm);`render`(transform + 各 provider convertMessages)。验收:render 在孤儿/aborted/并行 tool_call/跨 provider id 样本上产 provider-**合法**序列。

**P2 — 主 turn 写树 + 捕获 stopReason(前置)。** message-end 即时落 `message` entry;assistant entry 带 `stopReason`;turn end 落 `leaf`。验收(**语义等价闸门**,评审 B1):`render(build_context) ≡ legacy live list` 在**归一化投影**下(剥注入/无 snip 后),限"无压缩 turn";列明哪些维度在/不在关系内。

**P3 — resume 走 `build_context`,删快照。** `restore_session` 只读树;无 `session.jsonl` 才 legacy 迁移。验收:resume 续聊逐字等价;**abort turn 走 drop-and-retry**(接受半成品丢失);P3 不含 compaction-resume(挪 P4)。

**P4 — compaction-as-entry + 删 snip。** 删 `_run_compression_pipeline`;`compaction` entry(`firstKeptEntryId` 两区 fold);auto-compaction 触发改写树。验收:compaction 前后 resume 等价;可从 compaction 前任意 entry fork。

**P5 — 注入 → `custom_message`。** skill listing/memory recall/finished-task → `custom_message` entry(dedup 从树派生);删原地 `append_to_last_user`。验收:注入在树里、resume 重建一致、不改既有 user 消息。

**P6 — 子会话 = child session(§7)。** 含 child-sid 铸造、父子链接 = `agent` tool_call + bounded toolResult（后台完成 = 合成 `custom_message`，§7.2，**无 off-path entry**）、child `session_start.parentSession`、`children/parent/siblings` 派生索引、`trajectory_id` 显式串、per-session 单写者 lock。验收:spawn/resume/background/permission/导航契约测试全过,旧 `agent-001` 只能作为兼容别名。

**P7 — CLI(§8)。** Control 分支 + 命令 + 参数。

**P8 — 迁移 + 删旧主路径。** flat/v2 `messages.json`→`session.jsonl`(摘要正文优先从 v2 取,评审 m13);trajectory 重写从树派生;**删** `messages.json` 主存、双 `MessageStore`、wire 双轨。验收:新 session 只依赖 `session.jsonl`;禁 legacy 后测试绿。

### PR 顺序
```text
PR-1 docs(本文档) → PR-2 SessionManager+中立 schema+render 双移植+测试
PR-3 主 turn 写树 + stopReason 捕获 + 语义等价闸门
PR-4 resume 走 build_context（删快照） → PR-5 compaction-as-entry（删 snip）
PR-6 注入→custom_message → PR-7 子会话 child session（parent/child index + lock/trajectory_id）
PR-8 CLI → PR-9 迁移 → PR-10 删旧主路径（messages.json/MessageStore/wire 双轨）+ trajectory 重写
```

---

## 10. 迁移

- flat JSON / v2 `main/messages.json` → `session.jsonl`;**摘要正文/post-compaction 消息优先从 `messages.json` 取**（wire 的 **compaction 事件**只存计数、无摘要正文，评审 m13）。
- v2 `agents/<agent-id>/` → 独立 child session；父补 `agent` tool_call/tool_result（bounded，`details.childSessionId`），child `session_start.parentSession.agentId=<legacy>`；旧 `task_id`/`agent_id` 保持 alias 但不再是权威。
- wire **`llm_request.messages`**（raw wire，**非** compaction 事件）是 post-注入/post-压缩的全文,**不可直接当中立 user 消息导入**(会把 ephemeral 注入变永久史,评审 m13);仅作 debug-trace 导入。（与上一条不矛盾:compaction 事件只存计数，llm_request 存被污染的全文——两种不同 wire 记录。）
- 只 append/新建,不删旧文件;迁移失败不破坏原文件;报告不可精确恢复项。
- 命令:`nanocode sessions migrate [<id>]` / `inspect <id>`。

---

## 11. 风险与待决

**已消解(靠全-Pi 决策)**:B1 等价闸门(改语义等价 + 删 snip 后投影干净)、M2 注入(→`custom_message`)、M3 快照(删 snip + 注入入树 ⇒ render 纯确定 ⇒ 无需快照)。

**仍需经营**
- **render 合法性(评审 B3)**:必须同时移植 per-provider `convertMessages`,否则发非法 payload。测试矩阵:Anthropic/OpenAI × 并行 tool_call / 孤儿 / aborted(含已落 tool_result 的 inverse-orphan)/ context-clear-mid-batch / thinking 后悬空。
- **stopReason 前置(评审 B2)**:P2 前两个 backend 必须捕获,否则 drop-and-retry 无源数据。legacy entry 默认 `stop`(旧 abort turn 无法追溯丢弃)。
- **单写者 lock（评审 B4 并发 + 第一写竞态）**：B4 的 leaf-move 矛盾已由 §7.2 消解；但共享 worktree 多进程仍须强制单写者。**owner-PID-in-`state.json` 是 TOCTOU、关不住第一写竞态**（全仓零 flock）——须用**原子获取**：`O_EXCL` 创建 `<session>/.lock` 或 `fcntl.flock` on `session.jsonl`，append 前获取；陈旧检测靠 PID 存活 + lock mtime；第二个获取者**fork 新 session**。
- **child index 漂移**：权威 = child `session_start.parentSession`（§7.2）；父 toolResult `details` 仅 cache，加载时对账，冲突以 child header 为准、父链接标 orphan/diagnostic，不静默合并 transcript。
- **后台完成时机（branch + idle，评审命中）**：① 结果须 pin 到 **spawn 分支**而非 live leaf（否则 `/rewind`|`/fork` 后落错分支）；② 父 idle 时 background done-callback 须打断 stdin / 入队 nextTurn，被动 `custom_message` 不自动唤醒 idle 父——详见 §7.2。
- **trajectory 跨会话拼接（评审命中）**：child 是独立 `sessions/<childId>/`，旧的"glob 同 `session_id` 下 `agents/*/wire.jsonl`"免费 join 没了。重写后的 deriver 须以 parent sessionId 为 root，经 `children(parent)`/`parentSession` 回指枚举 child `session.jsonl`，用 child `session_start.trajectoryId`（=traj_parent）+ 父 `agent` tool_call entry 作 link 点，把多文件拼成一条 trajectory。
- **RuntimeEvent 边界（评审命中，§1 非目标"不动"）**：message-end 仍发**同一条** RuntimeEvent；新增"树折叠器"消费**同一流**追加 `session.jsonl`（**不**另起 out-of-band 第二写者）；退役的 wire durable 契约 guard 由**等价的 `session.jsonl` schema guard** 在同一流上替换——否则违背"RuntimeEvent 架构不动"。
- **thinking 同模型重放需 beta header（评审命中）**：回放早先（非末轮）带签名 thinking + tool use 交错，Anthropic 要求 `anthropic-beta: interleaved-thinking-2025-05-14`；backend 须新捕获 per-block signature 并发该 header，否则回放历史签名 thinking 会 400。
- **注入 dedup 跨 compaction（评审命中）**："dedup 从树派生"须**按子项**（skill 名/task id）、且只把落在最新 compaction `firstKeptEntryId` **之后（KEPT 区）**的 `custom_message` 算"已注入"（被折进摘要的不算），复刻现有"compaction 后重置注入"语义，否则 skill-listing 跨 compaction 会漏注入或重复。
- **trajectory 重写(评审 m2)**:`project.py`+`metrics.py` 两个消费者 + ~7 个锁 schema 的测试,排期单列。
- **行为改变(须接受)**:① abort turn drop-and-retry(丢半成品)② 删 snip ⇒ 先给 `read_file` 加 per-read cap、且更早/更多走 summary compaction(上下文管理变粗)。

**仍待你勾的小决策**
- 子会话:扁平+双向指针(本文档默认;不物理嵌套,不引入 OpenCode DB/API)。
- `/fork` 默认 in-file(本文档默认)。

> **thinking 已定调（不再 open）**：默认存全（thinking + signature/redacted），render 按 §4⑦ 严格 gate（同模型重放、跨模型降级/丢弃、aborted 整条不发）。即"存全事实、render 严格 gate"，照 Pi（commit `9ccfcd7`，用户核验）。

---

## 12. 与 Pi 的分歧（自证）

| 项 | Pi | nanocode | 依据 |
|---|---|---|---|
| 子 agent | `--no-session` 子进程 | child session(resumable) | nanocode background/resumable subagent |
| child-session 索引/导航 | 无 core subagent tree | 父 `agent` tool_call/tool_result（`details.childSessionId`）+ child `session_start.parentSession` + 派生 `children/parent/siblings` | 吸收 OpenCode child-session 闭环,但以 JSONL 为权威、链接走工具消息（非独立 entry、非 DB） |
| permission/task | 无 | `permission_decision`/`task_update` entry | nanocode 审计 |
| 压缩 | summary-compaction-as-entry | **照搬**(删自有 snip) | 求纯确定 render |
| 注入 | `custom_message` entry | **照搬** | M2 |
| 多 provider | 中立 + render | **照搬** | 同构 |
| id | uuidv7 8 字符截断+重试 | 全长 uuidv7 式(进程安全生成) | Python `>=3.11` 无 stdlib uuid7 |
| 存储 | 单文件+header 指针 | 目录式 | artifact/子会话 |
| 文件纪律 | `_rewriteFile`(非纯 append) | 同(可周期 GC) | Pi 应用层就这样 |

---

### 引用锚点
**Pi 应用层** `packages/coding-agent/src`:`core/session-manager.ts`(SessionManager 749-859、custom_message 1061-1068、getBranch 1150、loadEntriesFromFile 466、_rewriteFile 872);`core/agent-session.ts`(custom_message 持久化 506-515、appendMessage 516-523、stopReason 531、getBranch 1654/1817);`core/compaction/compaction.ts`(compaction entry 104-105、summary message 90、prepareCompaction 644-666、convertToLlm 583);`core/bash-executor.ts:124`(bash 截断；read/find/grep 亦在工具时 cap);`packages/coding-agent/examples/extensions/subagent/index.ts:288`(--no-session)。
**Pi 核心** `packages/agent/src/harness`:`session/jsonl-storage.ts`(leaf entry 226-244、leafIdAfterEntry 109-111);`session/session.ts`(buildSessionContext 42-77、moveTo 246-265);`messages.ts:120-164`(convertToLlm);`agent-harness.ts`(navigateTree 817-838、save-point 484-527);`types.ts:235-420`(union+中立 Message)。`packages/ai/src/providers`:`transform-messages.ts:60-217`;`anthropic.ts:1113-1146`、`openai-completions.ts:747-991`(convertMessages 合法性)。
**OpenCode 对照**（baseline `sst/opencode@826419127ae0c2b742b9db866c4a9afb27a5ae2c`，行号已逐条本机核验；**仅吸收 child-session 闭环，SQLite/HTTP/API 不入 nanocode 权威链**）：
- child 创建 `packages/opencode/src/tool/task.ts:146-162`（`sessions.create({parentID: ctx.sessionID, title, agent, permission})`）；`task_id` 续跑同一 child `:121-123`（`sessions.get(SessionID.make(task_id))`，参数语义 `:47-50`）。
- bounded `<task>` tool_result `:64-79`（`renderOutput`，模板体 70-78）；父子链路 metadata `:175-185`（`{parentSessionId, sessionId(child), model, background}`）；foreground completed 返回 `:320-324`。
- 后台回注父会话 `:206-233`（`inject()`→`ops.prompt({sessionID:parent, parts:[{type:"text",synthetic:true,…}]})`）；BackgroundJob（jobId==childSessionId）调用点 `:246-276`，实现 `packages/core/src/background-job.ts`（start 201-253 / extend 255-289 / wait 291-300 / waitForPromotion 302-307 / cancel 336-357），opencode 侧 re-export `packages/opencode/src/background/job.ts:2-33`。
- children/parentID 导航 `packages/opencode/src/session/session.ts:638-646`（`children(parentID)` 按 `parent_id` 过滤，接口声明 491）、`:213-233`（runtime `Info.parentID`，220）、`:1022-1024`（`listByProject` 以 `roots`→`parent_id IS NULL` opt-in 隐藏 child；`listGlobal` 600）。
- 权限派生 `packages/opencode/src/agent/subagent-permissions.ts:18-34`（deny 并集 + `external_directory` 继承，默认 deny `todowrite`/`task`），调用点 `task.ts:128-132`；child 跳过标题 `session/prompt.ts:183`。
- 注：core 包**无** `children()`/list-by-parent（导航唯一权威在 `session/session.ts`）；SQLite 表（`core/session/sql.ts:30,63`、`schema.ts:25-28`）、HTTP（`server/routes/**`）仅作对照、不入权威。
**nanocode 现状**:`agent/engine.py`(双 store 244-262、setter 509-529、restore 549-577、注入 640-737);`agent/compaction.py`(snip/microcompact 70-168);`agent/anthropic_backend.py`(注入 95-96/203、thinking 收集 138);`agent/openai_backend.py`(finish_reason 258-279、raw args 124-126);`agent/context_builder.py`(faithful 54-82);`trajectory/{project,metrics}.py`;`entrypoints/cli.py:568`(Control 占位)。
