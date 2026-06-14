# nanocode 上下文拼接与压缩改造建议报告：CLI 源码对照

本文的目标不是单纯横评 CLI，而是为 nanocode 的上下文拼接与压缩改造提供源码证据。分析只按源码行为，不按公开材料推断。Claude Code 使用用户提供的本地源码 `/Users/jyxc-dz-0101321/exam_project/Claude-Code`；其他项目使用本次分析拉取到 `/private/tmp` 的源码快照。报告不复制大段实现，只引用文件、函数和行号来定位证据。

## 1. 范围与结论

覆盖对象：

- nanocode：当前仓库 `/Users/jyxc-dz-0101321/exam_project/nanocode`
- Claude Code：`/Users/jyxc-dz-0101321/exam_project/Claude-Code`
- OpenAI Codex CLI：`/private/tmp/codex-src`
- Gemini CLI：`/private/tmp/gemini-cli-sparse`
- OpenCode：`/private/tmp/opencode-sparse`
- Pi coding agent：`/private/tmp/pi-src`，npm 包 `@earendil-works/pi-coding-agent`，commit `17721d5`
- KimiCode：`/private/tmp/kimi-code-src`，npm 包 `@moonshot-ai/kimi-code`，commit `1c65cbf`
- Aider：`/private/tmp/aider-sparse`

核心结论：

| CLI | 上下文源模型 | 请求前拼接 | 压缩触发 | 压缩形态 | 工具结果策略 | 主要优点 | 主要代价 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| nanocode | append-only 会话树 / branch entries | branch -> fold -> AgentMessage -> provider render | 上一轮输入 token 超过有效窗口 85% | 写入 `CompactionRequested` 条目，后续 fold 只保留 summary + suffix | 单工具结果超过 30KB 落盘并替换摘要 | 可恢复、可 fork、可审计，状态很清楚 | 对 compaction 请求本身过长、summary 膨胀、缓存命中优化处理较弱 |
| Claude Code | 消息数组 + compact boundary + per-request projection | 每次 query 先取 boundary 之后消息，再做预算、snip 入口、microcompact、collapse 入口、autocompact | 有效窗口扣除 summary 输出预留后，距离阈值 buffer 约 13k | full/partial/session-memory compact，summary + kept tail + 附件回灌 | tool-result budget、microcompact、cache edit、老结果清空、长附件回灌 | 工程防线最全，抗 prompt-too-long、缓存、附件恢复能力强 | 状态变换多，feature gate 多，理解和验证成本最高；本地源码中 snip/collapse 是 no-op |
| Codex CLI | `ResponseItem` history + compaction window | normalize history 后送模型；pre-sampling 和 mid-turn 都可能 compact | 配置范围内按模型限制和 token status | local/remote `/responses/compact`，替换 history 并推进 window id | function output payload 截断，必要时删最老 item 重试 | 和 Responses 协议结合紧，pre/mid/manual 多路径 | 对本地 fork/tree 语义不如 nanocode 显式，依赖 provider 能力较深 |
| Gemini CLI | 经典 chat history + 新 context graph 双轨 | context-management 开启时走 graph 渲染；否则 classic compression | 默认超过 token limit 50%，也支持强制 `/compress` | summary user message + model ack + kept tail；新架构另有文件级压缩/蒸馏 | function response 逆序预算 50k，超出写 temp file 占位；另有 masking | 上下文工程最细，能处理文件级和 graph 级优化 | 两套路径共存，行为复杂，summary 校验多一次模型调用 |
| OpenCode | DB MessageV2 stream + compaction part | 读取消息后 `filterCompacted` 重排为 compact prompt、summary、tail、continue | overflow policy + assistant 完成后自动创建 compaction | 用户消息里写 compaction part，summary 后保留 tail | 老 tool part 可标记 compacted，compaction 输入中工具输出截到 2k chars | compaction 是一等消息，事件/DB/fork 语义较清楚 | token 估算偏工程近似，compaction 请求 overflow 后处理较保守 |
| Pi | session tree entry + AgentMessage | `buildSessionContext()` 折叠 branch，再 `transformContext` hook，最后 `convertToLlm()` | 超过 `contextWindow - reserveTokens`，或 provider overflow | 写入 compaction entry；支持 previous summary 增量、turn prefix summary、扩展接管 | shell 输出可标记截断；压缩 summary 附 read/modified files | 和 nanocode 结构接近，但自动 overflow recovery、扩展 hook 更成熟 | token 估算较启发式；默认 compaction request 缺少多轮 prompt-too-long 降级重试 |
| KimiCode | in-memory ContextMemory + record/replay | context history 先 microcompact，再 projector 过滤 partial/空 assistant、合并相邻 user | 85% 阈值或预留 50k context；overflow 强制 block compact | full compaction 替换内存 history：summary assistant + recent tail；失败可重试 | 实验 micro compaction 清旧 tool result；PostToolUse hook 只给 2k tool output | retry/telemetry/overflow 分类完整，Kimi provider 适配强 | 压缩会改写内存 history，不如 nanocode append-only tree 易审计 |
| Aider | Prompt chunk builder | system/examples/files/repo map/done history/current/reminder 分块拼接 | done history 超预算后台总结；整体超上下文则提示用户 | 老 done history summary + recent tail | 主要靠文件块和 repo map 预算，不是通用 tool-result compaction | 简洁、可解释，repo map 很强，用户控制明确 | 自动恢复/自动 compact 能力弱，更依赖用户 `/drop`、`/clear` 等操作 |

## 2. nanocode

### 2.1 上下文拼接

nanocode 的核心不是维护一份可变 flat transcript，而是维护 append-only session tree，然后在请求前做确定性投影。源码在 `src/nanocode/session/context.py` 顶部注释已经定义 canonical pipeline：`get_branch -> fold -> AgentMessage[] -> convert_to_llm -> render(provider)`（`/Users/jyxc-dz-0101321/exam_project/nanocode/src/nanocode/session/context.py:1-8`）。

实际拼接分四层：

1. `fold()` 对 branch 上的 entries 做折叠：普通标量状态按 last-write-wins；压缩相关 entry 使用 “summary + firstKeptEntryId 之后的原始消息” 两段式视图（`context.py:45-114`）。
2. `convert_to_llm()` 把 compaction/branch summary 转成 user message，并为 summary 加 prefix/suffix；custom message 保持原始内容（`context.py:126-146`）。
3. `project_request()` 读取渲染后的 tree，再追加 volatile tail：Anthropic 的 system 是 out-of-band，OpenAI 的 system 被放入 messages（`src/nanocode/session/agent.py`）。
4. per-turn 的 `persist=none` 内容、环境和 git 状态不写入 tree，而是通过 user system-reminder 或 `ContextRuntime` pack 注入（`src/nanocode/session/agent.py`）。

这意味着 nanocode 的“真实会话状态”和“某次请求的上下文投影”是分开的：前者可持久化、可 fork；后者允许带 volatile 环境上下文。

### 2.2 压缩策略

自动压缩触发在 `compact()` 附近：如果 `last_input_token_count > effective_window * 0.85`，会触发自动 compact（`session.py:241-281`）。保留尾部预算由 `max(4000, 10% effective_window)` 决定，并优先在 user boundary 切分；找不到理想切点时退到最后一个 user（`session.py:283-319`）。

压缩结果不是直接覆盖历史，而是写入 `CompactionRequested` entry。下次 `fold()` 根据 `firstKeptEntryId` 只投影 summary 和被保留 suffix（`context.py:45-114`）。构造请求时也没有旧式 flat fallback，而是从 canonical tree 重建并 provider render（`session.py:489-509`）。

工具结果侧，旧版 snip/microcompact 已移除，当前压缩只做 summary-compaction-as-entry 以及单个工具结果上限（`/Users/jyxc-dz-0101321/exam_project/nanocode/src/nanocode/agent/compaction.py:1-12`）。超过 30KB 的 tool result 会保存到磁盘：shell 保留最后 200 行，其他工具保留前 200 行，并在上下文里放摘要占位（`compaction.py:25-49`）。

### 2.3 优劣

优点：

- 可解释性强：session tree 是唯一事实源，compaction 也是 entry，恢复、审计和 fork 都比较自然。
- provider 适配边界清晰：Anthropic/OpenAI system 注入差异集中在 projection/render 层。
- 行为确定：同一 branch 在同一运行时上下文下能复现相同折叠结果。

缺点：

- 对 compact prompt 自身过长缺少 Claude/Codex 那样的 prompt-too-long retry。
- 没有 summary 膨胀检测：Gemini 会在压缩后 token 反而膨胀时拒绝；Claude/Codex 会记录压缩前后 token。
- 工具结果策略偏粗：只有单结果 30KB cap，没有 OpenCode/Gemini 那种按全局预算逆序保护 recent outputs 的策略。

## 3. Claude Code

本节补充参考了《上下文压缩管理》一文（`https://www.xuanyuancode.com/learn-claude-code/tutorials/cc8b`）。文章的主判断是正确的：Claude Code 不是“快满了做一次摘要”，而是在主 query 链路里放了多层上下文治理。源码校验后需要加两点限定：第一，文章描述的 snip 和 context collapse 在这份本地源码里确实有调用入口，但 `snipCompact.ts` 与 `services/contextCollapse/index.ts` 都是 no-op；第二，真正稳定可落地的当前能力是 tool-result budget、microcompact、autocompact、reactive compact、compact boundary、post-compact rehydration 和 prompt cache 分界。

### 3.1 上下文拼接

Claude Code 在 `query.ts` 中按请求动态构造 messages：先从 `getMessagesAfterCompactBoundary(messages)` 取最近 compact boundary 之后的消息，然后套 tool-result budget、可选 snip、microcompact、context collapse，再进入 autocompact（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/query.ts:380-462`）。如果 autocompact 发生，query 会 yield compact 后的新 messages 并继续使用 compacted list（`query.ts:468-550`）。

API 之前还会做消息规整：`normalizeMessagesForAPI()` 会重排附件，剔除 virtual/progress/system message，并处理非法媒体或工具引用（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/utils/messages.ts:1989-2070`）。`getMessagesAfterCompactBoundary()` 从最后一个 compact boundary 后切片，并可投影 snipped view（`messages.ts:4643-4656`）。

从文章的“分级压缩”视角看，这条链路可以拆成六层：工具结果预算裁剪、snip 入口、microcompact、context collapse 入口、autocompact、reactive compact。源码顺序与文章一致：tool-result budget 在 microcompact 前运行（`query.ts:384-409`），snip 在 microcompact 前运行（`query.ts:411-425`），collapse 在 autocompact 前投影，意图是如果折叠后低于阈值就避免全局摘要（`query.ts:443-462`），最后 autocompact 才可能真正生成 compacted messages（`query.ts:468-550`）。但本地实现层面，`snipCompactIfNeeded()` 只返回原 messages（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/services/compact/snipCompact.ts:1-10`），`isContextCollapseEnabled()` 固定返回 false，`applyCollapsesIfNeeded()` 也返回原 messages（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/services/contextCollapse/index.ts:32-43`）。

### 3.2 窗口与触发

默认模型上下文和 compact 输出预算在 `utils/context.ts`：默认 context window 200k，compact max output 20k，并支持 1M/override 逻辑（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/utils/context.ts:8-12`, `:51-95`）。

`autoCompact.ts` 先计算 effective context window：模型 context 减去 summary 输出预留，预留值是 `min(model max output, 20k)`（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/services/compact/autoCompact.ts:28-49`）。自动阈值不是固定百分比，而是 `effective window - 13k buffer`（`autoCompact.ts:62-90`）。它还支持环境变量/设置禁用（`autoCompact.ts:147-158`）、递归 guard/context-collapse gate/token check（`autoCompact.ts:160-239`），并在连续 3 次失败后熔断（`autoCompact.ts:241-351`）。

### 3.3 压缩实现

`compactConversation()` 是主路径。压缩前会移除图片/文档内容（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/services/compact/compact.ts:145-200`），也会剥掉重新注入过的 skill attachments（`compact.ts:211-223`）。如果 compact 请求本身 prompt-too-long，`truncateHeadForPTLRetry()` 会按 API round group 删除最老消息，并在 assistant-first 时补 synthetic user marker（`compact.ts:243-291`）。

压缩后的消息顺序是 boundary、summary、kept messages、attachments、hook results（`compact.ts:330-338`）。主流程还会执行 hooks、流式生成 summary、对 prompt-too-long 重试、清 read-file cache、恢复文件/agents/plans/skills/deferred tools/MCP attachments、记录 token、执行 post hooks（`compact.ts:387-748`）。其中 summary loop 的 prompt-too-long retry 在 `compact.ts:445-491`，post-compact attachment 创建和回灌在 `compact.ts:517-585`，compact boundary 和 summary user message 在 `compact.ts:596-624`，真实 post-compact token estimate 和 prompt-cache break 通知在 `compact.ts:626-704`。

compact boundary 是 Claude Code 区分“完整 transcript”和“模型可见最近窗口”的关键。`createCompactBoundaryMessage()` 会生成 `system/compact_boundary` 并记录 trigger、preTokens、last pre-compact parent 等 metadata（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/utils/messages.ts:4530-4555`）；`getMessagesAfterCompactBoundary()` 每次只从最后一个 boundary 开始切片（`messages.ts:4631-4656`）。持久化恢复还要处理 preserved segment：`applyPreservedSegmentRelinks()` 会寻找最后一个带 preservedSegment 的 boundary，并在内存里重新链接 head/tail（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/utils/sessionStorage.ts:1839-1888`）。大 transcript 加载时还会跳过 pre-boundary 旧内容、保留 session metadata，并在遇到 compact boundary 时清空旧 collapse commit/snapshot（`sessionStorage.ts:3516-3706`）。

这也是它和 nanocode 最大的结构差异：nanocode 把 compaction 写成 branch entry，fold 时天然形成 summary + suffix；Claude Code 则把 boundary 作为 flat transcript 中的切片锚点，另外再靠 attachment/hook/cache/collapse 状态恢复运行时上下文。

除此之外，Claude Code 还有 partial compaction：支持 `from` / `up_to`，但源码明确体现其 prompt-cache tradeoff 不同（`compact.ts:765-940`）。

### 3.4 microcompact 与 cache edit

`microCompact.ts` 定义可压缩工具集合：read、shell、grep、glob、web search/fetch、edit/write 等（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/services/compact/microCompact.ts:40-50`）。`microcompactMessages()` 先尝试 time-based microcompact，再根据 feature/model/main-thread 选择 cached microcompact，否则 no-op（`microCompact.ts:253-293`）。

cached microcompact 使用 cache editing API，本地 messages 不变，只把 `cache_edits` 排入请求元数据（`microCompact.ts:296-395`）。time-based microcompact 则会在 idle gap 后把较老 compactable tool results 替换为 `[Old tool result content cleared]`，同时保护近期内容并维护 cache state（`microCompact.ts:401-529`）。同目录的 `snipCompact.ts` 在这份源码中是 no-op，因此不能把 snip compact 当成当前有效策略（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/services/compact/snipCompact.ts:1-10`）。

Prompt cache 也参与上下文策略。系统提示词里有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`，源码注释明确边界之前可以使用 global cache，边界之后是用户/会话相关内容，不应进入全局缓存（`/Users/jyxc-dz-0101321/exam_project/Claude-Code/src/constants/prompts.ts:105-115`）。构造 system prompt 时，静态段后插入该 boundary，再追加动态 sections（`constants/prompts.ts:560-576`）。所以 Claude Code 的上下文管理不是只压历史消息，还同时管理 cacheable prefix、动态 registry 内容和 compact 后的运行时回灌。

### 3.5 优劣

优点：

- 防线完整：阈值、熔断、prompt-too-long retry、post-compact 重估、hook 和附件回灌都覆盖了。
- 缓存友好：cached microcompact 不改本地消息，只改 provider cache，能减少 prompt cache 破坏。
- 对长会话友好：full compact、partial compact、session-memory compact、reactive compact 组合丰富；collapse/snipping 在这份源码中更多体现为预留接口和 feature-gated 架构位。

缺点：

- 复杂度高：压缩前后消息、boundary、attachments、hook results、cache state 都会影响最终请求。
- 行为 feature-gated，很难只看一处代码判断线上路径。
- 本地 messages 与 provider cache edit 可能分叉，调试时需要同时看 message list 和 pending cache metadata。

## 4. OpenAI Codex CLI

### 4.1 上下文拼接

Codex CLI 的上下文核心是 `ContextManager`，维护 oldest-to-newest 的 `ResponseItem` 列表、`history_version`、token info 和 reference context（`/private/tmp/codex-src/codex-rs/core/src/context_manager/history.rs:32-51`）。`record_items()` 只记录 API message，并处理/截断 item（`history.rs:90-105`）；`for_prompt()` 会 normalize，再丢掉不适合送模型的 item（`history.rs:107-114`）。

normalize 逻辑会保证 call/output 配对，并在模型不支持时剥离图片（`history.rs:323-336`）。工具/function output 会经 `truncate_function_output_payload()` 处理（`history.rs:338-374`, `:424-441`），API message filter 排除 system 与 compaction trigger（`history.rs:443-465`）。token 估算会把 base instructions 纳入计算（`history.rs:130-155`），`remove_first_item()` 会成对移除最老 item 和它的 counterpart（`history.rs:157-166`）。

### 4.2 触发与压缩路径

每个 turn 开始会先执行 `run_pre_sampling_compact()`，再记录 context updates（`/private/tmp/codex-src/codex-rs/core/src/session/turn.rs:137-157`）。采样输入来自 `clone_history().for_prompt()`（`turn.rs:218-225`）。采样后会计算 `auto_compact_token_status`；如果 token limit reached 且还需要 follow-up，会在 mid-turn 执行 auto compact，并把 initial context 注入到 last user 之前（`turn.rs:252-320`）。`auto_compact_token_status()` 本身按配置 scope 和模型限制判断（`turn.rs:733-776`），pre-sampling/previous-model/auto compact 会分派到 remote v2、remote 或 local（`turn.rs:788-936`）。

local compact 在 `compact.rs`：克隆 history，追加 compaction prompt，流式生成 summary；如果 compact 请求超上下文，就删除最老 item 后重试（`/private/tmp/codex-src/codex-rs/core/src/compact.rs:196-329`，其中重试在 `:252-264`）。成功后构造 compacted history、推进 compaction window id、替换 session history 并重算 token usage（`compact.rs:293-321`）。`InitialContextInjection` 区分 manual/pre-turn 和 mid-turn：manual/pre-turn 清 reference context，mid-turn 在 last user 前注入 initial context（`compact.rs:51-66`）。telemetry 会记录 compact 前后 active tokens 和 strategy “Memento”（`compact.rs:375-416`）。

remote compact 有两条：`compact_remote.rs` 使用 `/responses/compact`，先裁剪 function-call history，再处理返回的 compacted transcript、推进 window id、替换 history（`/private/tmp/codex-src/codex-rs/core/src/compact_remote.rs:163-283`）。`compact_remote_v2.rs` 设定 retained message budget 64k 和 stream retries 2，并克隆 history、trim outputs、带 `CompactionTrigger` 构造请求（`/private/tmp/codex-src/codex-rs/core/src/compact_remote_v2.rs:49-54`, `:198-243`）。

### 4.3 优劣

优点：

- 和 Responses 协议高度一致：`ResponseItem`、function call/output 配对、remote compact 都是协议级结构。
- pre-sampling 与 mid-turn 都能 compact，避免只在 turn 边界才处理溢出。
- compact prompt 自身过长时有删除最老 item 的恢复路径。

缺点：

- 状态替换比 nanocode 的 append-only compaction entry 更不直观，审计和 fork 需要依赖 window id 与 history version。
- remote compact 依赖 provider endpoint；跨 provider 或离线场景下能力不均衡。
- 工具输出处理更像 payload 截断，不像 Gemini/OpenCode 那样有更细的“保护近期、压缩旧文件/旧工具结果”策略。

## 5. Gemini CLI

### 5.1 双路径：classic chat compression 与 context graph

Gemini CLI 当前有两套上下文管理路径。`GeminiClient` 请求前先看 `getContextManagementConfig().enabled`。开启时走 `ContextManager.renderHistory()`，得到 durable `newHistory`、API history、pending API history，并把 `newHistory` 写回 chat；pending request 只为本次 API late-bind 到 `apiHistoryOverride`（`/private/tmp/gemini-cli-sparse/packages/core/src/core/client.ts:643-673`）。未开启时走 classic `tryCompressChat()`（`client.ts:688-693`）。

`ContextManager` 本身维护 pristine/active graph buffer、orchestrator、chat history 和 token calculator（`/private/tmp/gemini-cli-sparse/packages/core/src/context/contextManager.ts:26-63`）。它会把 durable `AgentChatHistory` 渲染成 graph nodes（`contextManager.ts:90-108`），预览 pending request 经过 pipeline 后的结果（`contextManager.ts:119-134`），等待 pipelines/hot-start 并评估 GC/distillation/normalization triggers（`contextManager.ts:147-161`），最后用 protection reasons/header/late binding 渲染 graph（`contextManager.ts:181-195`），并把管理结果提交回 master buffer（`contextManager.ts:205-218`）。

如果 context-management 开启但没有 contextManager，会退到 `AgentHistoryProvider.manageHistory()`（`client.ts:680-686`）。`AgentHistoryProvider` 会在超过 `maxTokens` 时总结 older portion，并对老消息与 grace-zone recent messages 使用不同限制（`/private/tmp/gemini-cli-sparse/packages/core/src/context/agentHistoryProvider.ts:34-115`）；文本和 function response 按比例截断（`agentHistoryProvider.ts:121-173`），按 retained token budget 切 keep/truncate，同时保持结构完整（`agentHistoryProvider.ts:180-214`）。

### 5.2 classic compression

`ChatCompressionService` 默认 compression threshold 是 token limit 的 0.5，并保留最新 30% chat，切分用字符启发式（`/private/tmp/gemini-cli-sparse/packages/core/src/context/chatCompressionService.ts:37-47`）。function response 预算是 50k tokens（`chatCompressionService.ts:50-53`）。

`findCompressSplitPoint()` 只在安全 user turn 上切，不切到 function response 中间；如果要压缩全部历史，则要求末尾是 terminal model message（`chatCompressionService.ts:60-100`）。function response 截断采用逆序预算：优先保留新的结果，旧的超预算后把完整输出写 temp file，并在上下文留下占位（`chatCompressionService.ts:137-237`）。

`compress()` 从 `chat.getHistory(true)` 取历史，未超过阈值且非 force 时 no-op（`chatCompressionService.ts:239-285`）。如果之前自动 summary 失败，它会走 truncate-only path（`chatCompressionService.ts:287-321`）。压缩时会拆成 history-to-compress 和 history-to-keep，并在 original/truncated summarizer input 间选择可放入窗口的版本（`chatCompressionService.ts:323-351`）。summary 生成后还有 snapshot-aware verification/self-correction 的第二次 LLM call（`chatCompressionService.ts:353-411`）。新 history 是 summary user message、一个 “Got it” model ack、kept tail；如果压缩后 token 反而膨胀则拒绝（`chatCompressionService.ts:431-481`）。

核心调用在 `GeminiClient.tryCompressChat()`：它调用 `compressionService.compress()`，summary 膨胀会设置 `hasFailedCompressionAttempt`；成功压缩时用 `startChat(newHistory, resumedData)` 重建 chat，truncate-only 时直接 `setHistory(newHistory)`（`/private/tmp/gemini-cli-sparse/packages/core/src/core/client.ts:1196-1251`）。agent local executor 也会在每 turn 前调用同一 compression service，并把返回的新 history 转成 turns 写回 chat（`/private/tmp/gemini-cli-sparse/packages/core/src/agents/local-executor.ts:339-343`, `:898-940`）。

### 5.3 文件级压缩

新 context-management 还包含文件级压缩：`ContextCompressionService.compressHistory()` 只有在 context management enabled 时运行（`/private/tmp/gemini-cli-sparse/packages/core/src/context/contextCompressionService.ts:108-115`），并保护最近 2 turns 的 read files（`contextCompressionService.ts:116-150`）。它会收集更旧的 `read_file`/`read_many_files` 输出、去重、hash，并通过批量模型决策判断是否压缩（`contextCompressionService.ts:152-232`），然后持久化每个文件的 compression state 并应用决策（`contextCompressionService.ts:50-59`, `:234-260`）。

### 5.4 优劣

优点：

- 上下文管理最“工程化”：classic chat summary、function response budget、graph pipeline、文件级压缩都有。
- summary 有校验/自修正，且会拒绝 token 膨胀结果。
- late binding pending request 可以减少 durable history 与本次请求投影之间的污染。

缺点：

- 双路径并存，排查一次请求到底走 classic compression、agent history provider 还是 graph pipeline 需要看配置。
- summary verification 额外消耗模型调用，延迟和成本更高。
- 文件压缩状态持久化在项目临时目录，语义比 append-only entry 难审计。

## 6. OpenCode

### 6.1 上下文拼接

OpenCode 的持久化单元是 DB 中的 `MessageV2` 和 parts。转模型消息时，`message-v2.ts` 会处理 user text/files/compaction prompt、assistant text/tool/reasoning；对已经 compacted 的 tool output 用 `[Old tool result content cleared]`；compaction 输入中剥离媒体，并能合成 interrupted tools（`/private/tmp/opencode-sparse/packages/opencode/src/session/message-v2.ts:142-426`）。

关键重排在 `filterCompacted()`：它倒序读取 chronological stream，找到最新 compaction 和 summary，然后重组成 `[compaction user, summary, retained tail, continue]` 供模型消费（`message-v2.ts:532-583`）。因为 compacted filter 会重排消息，`latest()` 使用 monotonic id 最大值判断最新消息，而不是列表末尾（`message-v2.ts:589-612`）。

prompt 主循环会先 `MessageV2.filterCompactedEffect()` 和 `latest()`（`/private/tmp/opencode-sparse/packages/opencode/src/session/prompt.ts:1145-1149`），如果当前 task 是 compaction 则进入 `compaction.process`（`prompt.ts:1202-1212`）。正常 assistant 完成后，如果 overflow，则创建自动 compaction（`prompt.ts:1214-1221`）。真正发送模型前还会经过 plugin transform、system assembly、model message conversion 和 processor（`prompt.ts:1325-1347`），processor 如果返回 compact 也会创建自动 compaction（`prompt.ts:1380-1389`）。最后异步 prune（`prompt.ts:1399-1400`）。

### 6.2 压缩与剪枝

常量集中在 `compaction.ts`：prune minimum 20k、protect 40k、tool output max chars 2k、默认 tail turns 2、recent token 保护 2k-8k（`/private/tmp/opencode-sparse/packages/opencode/src/session/compaction.ts:38-44`）。recent preserve budget 默认是 usable context 的 25%，再 clamp 到 2k..8k（`compaction.ts:90-95`）。

`isOverflow()` 交给 overflow policy 判断（`compaction.ts:178-188`），估算时把 `MessageV2` 转 model messages 后对 JSON 做 token estimate（`compaction.ts:190-196`）。选择压缩输入时，会按最新 N turns 和 token budget 选 head/tail，必要时可在 turn 内切分（`compaction.ts:198-249`）。`prune()` 从后往前走，保护 40k tokens 后把更老 tool parts 标记 compacted；超过 20k 才实际 prune（`compaction.ts:251-297`）。

`processCompaction()` 会校验 compaction parent、处理 overflow replay、选择 history、继承 previous summary、加入 plugin prompt/context，并在 compaction request 中剥离媒体、把 tool output 截到 2k chars（`compaction.ts:299-425`）。如果 compaction 自己也 overflow，会标 error 并停止（`compaction.ts:426-435`）。成功后把 `tail_start_id` 写入 compaction part（`compaction.ts:437-441`），自动 compaction 可 replay previous user 或注入 synthetic continue prompt（`compaction.ts:444-525`），最后发布事件和 summary text（`compaction.ts:528-551`）。`create()` 本身是写入一个带 compaction part 的 user message（`compaction.ts:554-576`）。

overflow policy 中 usable context 等于模型 input/context 减去 reserved output 和 compaction buffer，默认 buffer 是 `min(20k, max output)` 这类逻辑；当 auto compaction 关闭或模型无 context 时不触发（`/private/tmp/opencode-sparse/packages/opencode/src/session/overflow.ts:8-34`）。session fork/clone 会 remap compaction 的 `tail_start_id`（`/private/tmp/opencode-sparse/packages/opencode/src/session/session.ts:744-772`），消息分页读取保持 chronological list（`session.ts:857-880`）。

### 6.3 优劣

优点：

- compaction 是一等 message part，和 DB、事件、fork/clone 结合较自然。
- 工具结果 prune 有全局保护窗口，不只是单条截断。
- 自动 compaction 之后能 replay 上一个 user 或 synthetic continue，用户体验比“压缩后停住”更顺。

缺点：

- filter 后消息顺序不是原始 chronological order，调试时必须理解 `filterCompacted()` 的重排。
- token 估算靠 JSON 化后的 model messages，精度不如 provider-returned usage。
- compaction 自身 overflow 时源码路径偏保守，缺少 Claude/Codex 那种逐步删 head 重试生成 summary 的恢复。

## 7. Pi coding agent

### 7.1 上下文拼接

Pi 和 nanocode 在数据模型上很接近：会话是带 `id/parentId` 的 session tree，`CompactionEntry`、`BranchSummaryEntry`、`CustomMessageEntry` 等都是一等 entry（`/private/tmp/pi-src/packages/coding-agent/src/core/session-manager.ts:46-149`）。`buildSessionContext()` 会沿当前 branch 折叠 entries：状态类 entry 更新 thinking/model/tools；message/custom/branch_summary 进入 `AgentMessage[]`；如果存在最新 compaction，则先放 compaction summary，再从 `firstKeptEntryId` 开始保留旧消息和 compaction 后新增消息（`/private/tmp/pi-src/packages/agent/src/harness/session/session.ts:22-80`）。`appendCompaction()` 明确把 summary、`firstKeptEntryId`、`tokensBefore`、details、`fromHook` 写成 compaction entry（`session.ts:173-190`）。

模型请求前，Pi 的 agent loop 先允许 `transformContext` hook 改写 `AgentMessage[]`，再用 `convertToLlm()` 转 provider message，最后组装 `{ systemPrompt, messages, tools }` 发送（`/private/tmp/pi-src/packages/agent/src/agent-loop.ts:276-310`）。coding-agent 的 `convertToLlm()` 把 bash execution、custom、branch summary、compaction summary 都转成 user message；`!!` excluded bash 不进入上下文；compaction summary 用固定 `<summary>` 包裹（`/private/tmp/pi-src/packages/coding-agent/src/core/messages.ts:11-24`, `:140-195`）。harness 侧还把 `context` hook 接在 `transformContext` 上，允许扩展在请求前替换上下文（`/private/tmp/pi-src/packages/agent/src/harness/agent-harness.ts:421-433`）。

### 7.2 压缩策略

Pi 的默认压缩配置是 enabled、`reserveTokens=16384`、`keepRecentTokens=20000`（`/private/tmp/pi-src/packages/coding-agent/src/core/compaction/compaction.ts:115-125`）。触发判断是 `contextTokens > contextWindow - reserveTokens`（`compaction.ts:216-222`）。token 计算优先使用 assistant usage 的 totalTokens；如果没有 totalTokens，则 `input + output + cacheRead + cacheWrite`；新尾部消息用估算补齐（`compaction.ts:131-214`）。

切点选择比 nanocode 更灵活。`findValidCutPoints()` 不在 tool result 上切，允许在 user、assistant、custom、bashExecution、branch_summary/custom_message 等可转 user 的位置切；`findCutPoint()` 从尾部反向累加到 `keepRecentTokens`，必要时允许切在一个 turn 内，并记录 `turnStartIndex`（`compaction.ts:292-448`）。

`prepareCompaction()` 会跳过“刚 compact 完又 compact”的情况，找到前一次 compaction，把 previous summary 作为增量总结基础，并从上次 `firstKeptEntryId` 之后开始计算新边界；它还抽取 read/modified file operations，返回 `messagesToSummarize`、`turnPrefixMessages`、`previousSummary`、fileOps 和 settings（`compaction.ts:626-719`）。如果切在 turn 中间，`compact()` 会并行生成 history summary 和 turn-prefix summary，再合并成单个 summary；最后追加 read/modified files 列表（`compaction.ts:725-820`）。summary prompt 是结构化 checkpoint，要求保留目标、约束、进度、关键决策、下一步和关键上下文（`compaction.ts:454-485`）；有 previous summary 时使用 update prompt 并要求 preserve existing information（`compaction.ts:487-524`）。

手动 `/compact` 会 abort 当前 agent 操作、准备 compaction、触发 `session_before_compact` extension hook，允许扩展取消或直接提供 compaction，然后写入 session 并重建 agent state messages（`/private/tmp/pi-src/packages/coding-agent/src/core/agent-session.ts:1641-1753`）。自动压缩分两类：provider overflow 时会移除 agent state 里的 error assistant，compact 后自动 retry；阈值触发时 compact 但不自动 retry（`agent-session.ts:1788-1875`）。`_runAutoCompaction()` 和手动路径一样支持 extension hook，并在 overflow retry 前再次移除末尾 error assistant（`agent-session.ts:1881-2039`）。overflow 检测覆盖多 provider 错误模式、silent overflow 和 length-stop overflow，其中包含 Kimi For Coding 的 `exceeded model token limit` 模式（`/private/tmp/pi-src/packages/ai/src/utils/overflow.ts:1-154`）。

### 7.3 优劣

优点：

- 和 nanocode 的 session tree/compaction entry 很接近，迁移理念成本低。
- 比 nanocode 多了 overflow recovery：真实 provider 返回 context overflow 后能 compact 并自动 retry。
- `previousSummary` 增量更新、turn-prefix summary、fileOps 记录都能提高压缩连续性。
- extension hook 允许外部实现结构化 compaction，不必把所有策略写进 core。

缺点：

- token 估算中有 chars/4 启发式，准确性不如直接依赖 provider usage。
- 默认 compaction 生成本身没有 KimiCode/Claude/Codex 那种多轮缩减重试。
- extension hook 很灵活，但也会让最终上下文取决于运行时扩展，审计复杂度高于纯 deterministic fold。

## 8. KimiCode

### 8.1 上下文拼接

KimiCode 的核心上下文在 `ContextMemory`，维护 `_history`、token count、open tool steps、pending tool results 和 deferred messages（`/private/tmp/kimi-code-src/packages/agent-core/src/agent/context/index.ts:24-33`）。user/system-reminder 都以 message 追加，其中 system reminder 被包成 user 角色的 `<system-reminder>` 文本（`context/index.ts:39-60`）。loop event 会逐步写入上下文：step begin 创建 assistant placeholder，step end 根据 usage 刷新 `_tokenCount`，content/tool events 填充当前 open step（`context/index.ts:213-243`）。

模型投影是 `ContextMemory.messages -> project(this.agent.microCompaction.compact(messages))`：先走 micro compaction，再由 projector 过滤 partial/空 assistant，并合并相邻真实 user message，最后剥掉 context metadata（`context/index.ts:200-206`；`/private/tmp/kimi-code-src/packages/agent-core/src/agent/context/projector.ts:5-72`）。`trimTrailingOpenToolExchange()` 会裁掉尾部未闭合 tool exchange，避免 provider 看到不完整 tool call/result 对（`projector.ts:74-92`）。

### 8.2 压缩策略

KimiCode 默认 full compaction 配置：`triggerRatio=0.85`、`blockRatio=0.85`、预留 context `50_000`、保留最近最多 4 条 message、recent size 最多 20% context、overflow 缩减至少 5% context（`/private/tmp/kimi-code-src/packages/agent-core/src/agent/compaction/strategy.ts:5-25`）。`shouldCompact()`/`shouldBlock()` 同时看 85% 阈值和 reserved context 是否会被吃完（`strategy.ts:46-65`）。自动切点必须满足 `canSplitAfter()`：不能切在 user 后、不能切在带 tool calls 的 assistant 后，suffix 也不能从 tool result 开始（`strategy.ts:67-170`）。

`FullCompaction.begin()` 会算 compactedCount，无法切分就抛 `COMPACTION_UNABLE`；manual 会重置 turn 内 compact 次数，auto 受 `maxCompactionPerTurn` 限制（`/private/tmp/kimi-code-src/packages/agent-core/src/agent/compaction/full.ts:94-118`）。turn 前 `beforeStep()` 会检查自动压缩并在达到 block 条件时等待 compaction 完成；overflow error 总是 block（`full.ts:176-188`）。turn 主循环捕获 `APIContextOverflowError` 或 `CONTEXT_OVERFLOW` 后调用 `fullCompaction.handleOverflowError()`，然后继续 retry（`/private/tmp/kimi-code-src/packages/agent-core/src/agent/turn/index.ts:709-716`）。错误分类也会把 context overflow 单独标出（`turn/index.ts:1045-1064`）。

full compaction worker 克隆原 history，记录 `tokensBefore`，应用 completion budget 后，用被压缩 prefix 的投影加 compaction instruction 生成 summary（`full.ts:236-285`）。如果 provider 报 context overflow 或 summary 被截断，会调用 `strategy.reduceCompactOnOverflow()` 减少 compacted prefix，并最多重试 5 次（`full.ts:48-55`, `:259-304`）。成功后计算 `tokensAfter = estimateTokens(summary) + recent tokens`，记录 telemetry：trigger、before/after tokens、duration、compacted_count、retry_count 和 usage（`full.ts:320-341`）。最后 `context.applyCompaction()` 把内存 history 改写成一个 assistant summary 加 recent tail，并重新注入 goal reminder、触发 post-compact hook（`full.ts:342-349`；`context/index.ts:149-178`）。

micro compaction 是实验开关 `micro_compaction`。默认保留最近 20 条 message，只有 cache miss 超过 1 小时且 context 使用率至少 50% 时才检测；会把 cutoff 之前 token 足够大的 tool result 替换成 `[Old tool result content cleared]`，并记录 telemetry 的 before/after token 效果（`/private/tmp/kimi-code-src/packages/agent-core/src/agent/compaction/micro.ts:7-21`, `:46-77`, `:79-128`）。PostToolUse hook 只把非错误 tool output 的前 2000 字符传给 hook，避免 hook payload 过大（`/private/tmp/kimi-code-src/packages/agent-core/src/agent/turn/index.ts:681-703`）。

### 8.3 优劣

优点：

- 自动压缩链路完整：阈值、reserved context、overflow retry、summary 截断 retry、telemetry 都有。
- 切点安全规则清晰，避免 orphan tool result。
- micro compaction 是 projection 层变换，不直接破坏原始 context message，适合借鉴到 nanocode 的 render/projection 阶段。

缺点：

- full compaction 会直接改写 in-memory history 为 summary + tail；虽然有 records/replay，但不如 nanocode 的 append-only entry 天然可审计。
- recent 保留按 message count/ratio，不如 nanocode 按 user boundary 的语义直观，也不如 Pi 的 turn-prefix summary 对 split turn 解释充分。
- KimiCode 的 provider/flag/goal/hook 逻辑较深，照搬会提高 nanocode core 复杂度。

## 9. Aider

### 9.1 上下文拼接

Aider 的上下文拼接是 chunk builder 思路。`ChatChunks` 定义顺序为 system、examples、readonly files、repo map、done history、chat files、current messages、reminder（`/private/tmp/aider-sparse/aider/coders/chat_chunks.py:5-26`）。cache-control header 会加到 examples/system、repo/readonly、chat_files 等 chunk 上（`chat_chunks.py:28-55`）。

`format_chat_chunks()` 负责构造完整 prompt：system prompt、examples、已完成历史 summary、repo/read-only/chat file messages、current messages，以及预算允许时的 reminder（`/private/tmp/aider-sparse/aider/coders/base_coder.py:1226-1331`）。`send_message()` 追加 user message、格式化 messages、检查 token、预热 cache（`base_coder.py:1419-1433`）。

### 9.2 历史总结与 token 检查

当 `done_messages` 太大时，Aider 会启动后台总结（`base_coder.py:1002-1012`）。worker 总结 `done_messages`，但只有在历史未变化时才交换结果，避免并发覆盖新历史（`base_coder.py:1014-1034`）。每轮结束时，current turn 移到 done，并启动总结（`base_coder.py:1036-1046`）。

`ChatSummary` 默认最大 1024 tokens，`too_big()` 用 total 判断是否超限（`/private/tmp/aider-sparse/aider/history.py:7-18`）。递归总结时保留最近 tail，tail 最多占 max 的一半，并确保 head 以 assistant 结束；如果 summary + tail 放得下就返回（`history.py:27-96`）。`summarize_all()` 把 markdown transcript 放入 user message，让模型生成单条 user summary（`history.py:98-123`）。

模型预算在 `models.py`：`max_chat_history_tokens = min(max(max_input/16, 1024), 8192)`（`/private/tmp/aider-sparse/aider/models.py:355-358`）。repo map 预算是 `max_input/8`，clamp 到 1024..4096（`models.py:782-789`）。`RepoMap` 会在有其他文件时启用，且通过 tag 排序和二分控制 token 预算，误差 15% 内接受（`/private/tmp/aider-sparse/aider/repomap.py:103-155`, `:633-706`）。

如果最终 estimated context 超过 max input，`check_tokens()` 不会自动强行 compact，而是警告用户并询问是否继续，同时建议 `/drop`、`/clear`、缩小文件等操作（`base_coder.py:1396-1417`）。

### 9.3 优劣

优点：

- 拼接顺序极清晰，用户可以理解“哪些块进入上下文”。
- repo map 的预算控制和代码结构摘要很成熟，适合代码编辑任务。
- 后台总结使用 history unchanged check，避免异步 summary 覆盖新消息。

缺点：

- 更偏用户手动治理：上下文超限时提示用户，而不是像 Claude/Codex/OpenCode 自动恢复。
- 压缩对象主要是 done chat history；对工具结果、文件读取结果没有通用细粒度 compaction 层。
- summary 默认预算较小，对长任务保真度取决于总结质量。

## 10. 横向差异

### 10.1 事实源：tree、flat history、graph、chunk

nanocode、Pi 和 OpenCode 都把 compaction 作为持久化事实的一部分。nanocode 是 append-only branch entry + fold；Pi 也是 session tree entry + `firstKeptEntryId`，和 nanocode 最接近；OpenCode 是 DB message part + filter/reorder。nanocode/Pi 更像事件溯源，OpenCode 更贴近日志流和 UI 事件。

Claude Code、Codex、Gemini classic、KimiCode 更接近“请求前或运行时把 history 改写成新 history”。Claude Code 通过 compact boundary 维持切片边界；Codex 通过 compaction window id 维持窗口语义；Gemini classic 直接把 chat history 替换成 summary + ack + tail；KimiCode 在 `ContextMemory.applyCompaction()` 中把内存 history 改写成 assistant summary + recent tail。

Gemini context-management 是另一类：不是只压 transcript，而是把历史、文件、pending request 转成 graph，再由 pipeline 做 GC/distillation/normalization。Aider 则不是统一 transcript 管理，而是固定 chunk 顺序的 prompt builder。

### 10.2 触发策略

- nanocode：`last_input_token_count > effective_window * 0.85`。
- Claude Code：`model context - summary output reserve - 13k buffer`，并有禁用、递归 guard、熔断。
- Codex：按 auto compact scope、模型限制、server/estimated token status，在 pre-sampling 和 mid-turn 都可能触发。
- Gemini classic：默认超过 token limit 的 50%，也可 force。
- OpenCode：overflow policy 根据 usable context、reserved output、compaction buffer 判断；assistant 后自动创建 compaction。
- Pi：`contextTokens > contextWindow - reserveTokens`，默认 reserve 16k；provider overflow 时 compact 并 retry。
- KimiCode：85% 阈值或 reserved context 50k；overflow error 强制等待 full compaction 后 retry。
- Aider：done history 超预算后台 summary；整体超上下文时主要提示用户。

### 10.3 保留策略

- nanocode：summary + suffix，suffix 预算约 10% window，按 user boundary 切。
- Claude Code：summary + kept messages + 附件/hook/skill/MCP 回灌，partial compact 可按范围压。
- Codex：summary 替换 compacted history，推进 window；mid-turn 可注入 initial context。
- Gemini classic：summary user + model ack + kept tail；summary 失败后可 truncate-only。
- OpenCode：compaction part + summary + retained tail + continue，可在 turn 内切。
- Pi：compaction entry + previous summary 增量更新 + recent tail；若切在 turn 中间，会补 turn-prefix summary。
- KimiCode：assistant summary + recent tail；切点安全规则保护 tool call/result 边界，recent 由 message 数和 token ratio 控制。
- Aider：done summary + recent tail + repo map/current chunks。

### 10.4 工具结果与文件内容

Claude Code 最复杂：tool-result budget、microcompact、cache edit、time-based 清旧结果、附件回灌。Gemini 对 function response 做全局 50k 逆序预算，并有文件级压缩状态。OpenCode 对 old tool parts 做 prune 标记，并在 compaction request 把 tool output 截到 2k chars。KimiCode 的 micro compaction 会在 cache miss 且 context 使用率较高时清旧 tool result。Pi 的 compaction 会把 read/modified files 附进 summary details/text，但没有独立全局 tool-result budget。Codex 主要做 function output payload truncate。nanocode 目前是单工具结果 30KB cap 并落盘。Aider 主要靠文件块和 repo map 预算，而不是通用工具结果治理。

### 10.5 prompt-too-long 恢复

Claude Code 和 Codex 都有明确的 compact 请求 prompt-too-long 恢复：Claude 按 API round group 删除 head 重试；Codex 删除最老 item 重试。KimiCode 对 compaction 请求 overflow 或 summary 截断会减少 compacted prefix 并最多重试 5 次。Gemini 会在 summarizer input 太大时选择 truncated input，失败后走 truncation-only。Pi 默认没有多轮缩减重试，但 provider overflow 后会 compact 并 retry 主请求。OpenCode 如果 compaction 自身 overflow 会标错停止。nanocode 目前没有同等级的 retry 机制。

### 10.6 可恢复性与审计

nanocode 和 Pi 最强：compaction 不覆盖完整历史，而是 entry 化，fold/buildContext 规则明确；Pi 额外有 extension hook，灵活性更高但审计变量也更多。OpenCode 次之：compaction part 持久化且 fork remap `tail_start_id`，但 filter 后顺序重排需要额外理解。Codex 有 window id 和 history version，但 replacement history 让“原始事件流”不如 tree entry 直观。Claude Code 功能最全，但消息、附件、cache edit、hook 共同决定最终请求。KimiCode 有 records/replay，但 full compaction 直接改写内存 history。Gemini graph 管理能力强，但持久化状态和 pipeline 决策更难人工审计。Aider 最容易人工读懂 prompt chunks，但自动恢复能力有限。

## 11. 对 nanocode 的建议

### 11.1 总原则

1. 保留当前 canonical session tree + compaction entry 设计。它是 nanocode 相对 Claude/Gemini/Codex/KimiCode 最大的确定性优势，尤其适合 resume、fork、审计和测试。Pi 的源码进一步证明这条路线可行：它同样用 session tree entry、`firstKeptEntryId` 和 branch fold/buildContext 承载 compaction，但在自动触发和扩展 hook 上补了工程能力。

2. 不要把 Claude/Gemini/KimiCode 的运行时状态原样照搬。nanocode 应该把复杂策略落在 deterministic projection、显式 entry 或 `ContextRuntime` pack 上，避免 hidden mutation 破坏可审计性。

### 11.2 第一阶段：低风险可靠性补强

1. 增加 compact prompt 的 prompt-too-long retry。优先借鉴 KimiCode 的做法：保留原始 history，不断减少被送去总结的 prefix，并设置最大重试次数；也可以参考 Claude Code 的“按 API round 删除 head”或 Codex 的“删除最老 item 并重试”。结果仍写成显式 compaction entry，避免隐藏替换历史。

2. 增加 provider overflow recovery。Pi 和 KimiCode 都把 provider context overflow 当成自动 compact 的强触发：Pi compact 后 retry 主请求；KimiCode `handleOverflowError()` 会 block 等压缩完成后继续 turn。nanocode 可以在 backend 层把 context overflow 标准化为统一错误码，在 agent session 层触发 compact，然后重建请求重试一次。

3. 增加压缩前后 token telemetry 与膨胀拒绝。Gemini 的 inflated token rejection 很实用；Claude/Codex/KimiCode 的 pre/post token 记录也能帮助定位压缩收益。建议在 compaction entry 的 details 中记录 `tokensBefore`、`tokensAfter`、`summaryTokens`、`keptTailTokens`、`retryCount`、`trigger`。

### 11.3 第二阶段：压缩质量提升

1. 引入 Pi 式 previous summary 增量更新。当前 nanocode 每次 compact 更像“总结 prefix + 保留 suffix”；如果多次 compact，建议把上一条 compaction summary 显式传给 summarizer，要求 preserve existing information，再吸收新增 prefix。这比反复总结 summary 文本更可控。

2. 引入 Pi 式 turn-prefix summary。nanocode 当前优先按 user boundary 切，语义稳定；但在超大单轮工具调用或长任务中，完全按 user boundary 可能无法释放足够空间。可以保留“优先 user boundary”，但当必须切在 turn 内时，为被切掉的 turn prefix 单独生成小 summary，并合并进 compaction summary。

3. 把工具结果治理从“单条 30KB cap”升级为“全局 recent-protected budget”。可借鉴 OpenCode：保护最近若干 turn 或最近 40k token，把更旧 tool result 标记为 compacted；也可借鉴 Gemini 的逆序预算和落盘占位；KimiCode 的 micro compaction 可作为 projection-only 版本参考。但建议作为 deterministic projection 或显式 entry，保持可审计。

4. 对 post-compact rehydration 保持克制。Claude Code 的文件/skills/MCP 回灌很强，但复杂度高。nanocode 可以优先通过已有 `ContextRuntime` pack 和 ledger 实现“最近 read files / active skills / memory”回灌，不直接修改历史。Pi 在 summary 中记录 read/modified files 是更轻的替代方案，可以先做。

### 11.4 第三阶段：窗口与扩展能力

1. 如果未来支持频繁自动压缩，可引入 Codex 式 compaction window accounting。这样可以避免对已经 compacted 的窗口重复计数，也能更清楚地区分 pre-turn 和 mid-turn 压缩。

2. 谨慎引入 extension hook。Pi 的 `session_before_compact` 很强，允许扩展接管 compaction；但这会让上下文结果取决于运行时扩展。nanocode 若要支持，应把 hook 产物写入 compaction entry details，并在 replay 时可见。

3. 不建议第一阶段实现 Claude 式 cache editing。它收益高，但会引入“本地历史”和“provider cache 状态”分叉；这和 nanocode 当前的可恢复性目标冲突较大。

### 11.5 测试矩阵

给 compaction 策略补 deterministic fixture 测试：

- user boundary 切分、turn 内切分、turn-prefix summary。
- summary entry fold、连续多次 compaction、previous summary 增量更新。
- fork 后 `firstKeptEntryId` 仍指向正确 entry。
- provider overflow 后 compact + retry 只重试一次，且不把 overflow error 留在下一次请求上下文。
- compact prompt 过长 retry 能逐步减少 prefix，达到最大次数后给出明确错误。
- summary 膨胀拒绝或警告。
- 工具结果落盘、全局 tool budget、projection-only micro compaction 不改变 canonical tree。
- `ContextRuntime` packs 在 compact 后能重新注入，但不污染 session tree。

## 12. 总体判断

如果按“工程复杂度和抗异常能力”排序，Claude Code 和 Gemini CLI 最强；Claude 更偏运行时防线和 cache/附件恢复，Gemini 更偏 graph/context engineering。Codex CLI 的优势在 provider/protocol 一体化，pre/mid/manual compact 路径完整。KimiCode 的 full compaction retry、reserved context 和 micro compaction 很适合借鉴，但它的 in-memory history rewrite 不适合作为 nanocode 的事实源模型。OpenCode 在一等 compaction message 与自动 replay 上做得平衡。Pi 对 nanocode 最有参考价值：同样是 session tree + compaction entry，却补齐了 overflow recovery、previous summary、turn-prefix summary、fileOps 和 extension hook。Aider 则代表了 chunked prompt builder 路线，简单、可解释、强 repo map，但自动恢复弱。

nanocode 当前设计不是功能最多的，但它的 session tree 和 compaction entry 是很好的基础。建议改造方向是“保留 nanocode/Pi 的可审计事实源，吸收 KimiCode/Claude/Codex 的异常恢复，选择性引入 Gemini/OpenCode 的工具结果预算”。第一批最值得做的是：compact prompt retry、provider overflow compact+retry、压缩收益 telemetry、previous summary 增量更新、全局工具结果预算。这样能显著提升长会话可靠性，同时保留 nanocode 最关键的可恢复性和确定性。
