# docs/16 — nanocode → pi 收敛计划（激进版，直接 cutover、不留旧兼容）

状态：**权威设计 v1**（2026-06-12）。基于 6 子系统 pi/nanocode 双侧源码 gap-map + 综合架构师二次核验产出，
按用户定调改写为**激进模式**：重构期、不保留老旧兼容兜底、不做 flag-gated 双路径，删除项与替换项在同一变更系列落地。

源码基线（**两侧均以当前源码为准，旧 docs 的 pi 描述不再权威**）：
- pi: `earendil-works/pi` HEAD `1da903983ad72c60995507e813a00bb2bd6faf09`（本地 clone `/tmp/pi-src`）。
- nanocode: main HEAD `4de29bc`（docs/15 phases 0-8 已落地的状态，已第一手审计核对）。

## 0. 旧文档勘误（docs/13 / docs/15 相对 pi 当前源码）

1. **docs/13 的 pi 参照层已过时**。docs/13 基线 commit `9ccfcd7` 时 session 模型在 `packages/coding-agent` 应用层；
   pi 现已把 **session 树 + compaction + system-prompt + render + skills 整体下沉进可复用的
   `packages/agent/src/harness/`**（`agent-harness.ts` 1064 行、`session/session.ts`、`compaction/compaction.ts` 756 行），
   且 `packages/agent/src/index.ts` 显式导出 `AgentHarness/Session/compact/buildSystemPrompt/render/uuidv7/truncate` 全套。
   **今后 pi 对照的权威参照物 = `packages/agent/src/harness/`，不再是 coding-agent。**
2. **docs/13 的核心结论全部仍然成立**（compaction-as-entry 唯一 shrink、custom_message 注入、drop-and-retry abort、
   subagent 仍是 `--no-session` 子进程 example 扩展、8 字符截断 id `jsonl-storage.ts:35`）。锁定的偏离全部维持（§4）。
3. **pi 当前 HEAD 自带一笔债**：harness 一套 session/compaction 之外，coding-agent 应用层
   `agent-session.ts`(3135 行) 还维护**自己的一套** `SessionManager` + compaction(:1641)。对照时勿把 app 层那套当目标。
4. **docs/15 蓝图被 pi 演进直接验证**（见 §1）；过时的是 15-IMPL-roadmap 的「acceptance 达成」措辞——残留清单
   缺三条：core 仍 host 委托、`record_event` 零调用、AgentProfile 未上 live 路径。本文档接管这三条。

## 1. 总体判断

pi 当前三层（二次核验）：

| 层 | pi | 职责 | 触碰 session |
|---|---|---|---|
| 纯 loop | `agent.ts` + `agent-loop.ts` | state-in / events-out / tools-injected；全文只有 `sessionId` 透传（agent.ts:111/214/427） | 否 |
| **可复用 harness** | `harness/agent-harness.ts` | `handleAgentEvent`(:510-537) 是 **session 唯一写入者**（message_end → `session.appendMessage` :511-512，**无 try/except，存储失败沿 await 链上抛中止 turn**）；`executeTurn`(:553) turn shell；`compact()`(:708-762) | 是 |
| app shell | `coding-agent/agent-session.ts` | steering/extensions/model-cycle/slash 等产品语义 | app 自己一套（债） |

对 nanocode 的三个判断：

- ✅ **验证**：canonical 树作请求权威 + 每轮 `get_branch→fold→convert_to_llm→render`（fail-loud）+ compaction-as-entry，
  与 pi harness 同构，方向全对。
- ⚠️ **纠正（本文档的全部内容）**：边界错位。`AgentCore`(core.py) 既是 loop 又经 `host._tree_*` 直接写树
  （实测 **171 次 host 触达、~39 个不同成员**，高于 roadmap 估的 28）；本该当唯一写入者的
  `AgentSession.record_event`(session.py:71) **零生产调用**，typed `AgentEvent`(events.py 13 变体) **无任何构造点**。
  整条 pi 式事件脊柱是死代码。目标：**AgentCore 收敛为 pi 的 `Agent`（纯 loop、只 emit），AgentSession 升级为
  pi 的 `AgentHarness`（事件订阅者 = 唯一树写入者 + turn shell + compaction owner）。**
- 📦 **库存已齐，剩接线不剩发明**：`state.py`≈AgentContext、`events.py`≈pi AgentEvent（更富：stop_reason/usage/latency）、
  `record_event`≈handleAgentEvent、`ProviderAdapter`≈StreamFn、ContextRuntime+**ContextLedger（pi 没有）**。

## 2. 激进策略（与保守版的差异，定调）

**删的部分——不留兼容**：
1. **不做 flag-gated 双路径 / 灰度**。STEP D 直接 cutover，安全网 = 全量测试（uv 下 1348 收集全绿）+
   `verify_turn_consistency` turn-end 断言 + anthropic/openai/early-exec/batch 四路径专项测试。
2. **删除项并入替换变更，不做"后续清理"**：flat fallback（memory core.py:63-72 / skill bodies engine.py:894-901 /
   finished_tasks :935-948 / skill_listing :861-867）、`skills/listing.py:append_to_last_user`、plan_mode 的
   `_openai_messages[0]` 重写与 `_context_cleared` flag、`_load_messages/_dump_messages/_replace_messages` ownership shim、
   `_persist_state`/`_reload_task_state` 作 resume 权威——各自在其替换者落地的同一系列里死掉。
3. **engine 不留转发薄 shim**：translator 方法迁入 AgentSession 后，engine 上的同名方法直接删除、调用方一次性更新
   （保守版"可保留薄 shim（pi 也没删动词）"作废）。engine.py 终态走 roadmap §14 最后一条：不再是架构中心。
4. **`subagents/config.py` 退役**：`_resolve_effective` 收窄代数 + trust gate + `_filter_tools` 整体 port 进
   `agents/registry.py`（registry docstring 自己就是这么写的），dict API 删除，spawn.py / tasks_tool.py 调用方改 typed
   `AgentProfile`。两份重复的 allow-intersection（config.py:246-260 vs permissions.py:21-32）合一。
5. **PermissionEngine → PermissionContext** 内部替换并入 spawn cutover（原残留 #2 不再"按需后续"）。
6. **silent `except Exception: pass` 全部清除**：core.py:76/237（memory 注入消费）、engine.py:751-755（`_tree_event`）、
   engine.py:784（`_persist_state`）、engine.py:824（`append_compaction`）。message family 一律 fail-loud；
   telemetry/annotation 降级为 **observable**（sink.info/计数），不允许静默。
7. **ToolSpec 直接替换**，不做 adapter wrapper 过渡：registry.tool_definitions + execute._HANDLERS 两份全局合并为单一
   `TOOLS: dict[str, ToolSpec]`，调用方一次性更新。

**不删的部分——这些是正确性/安全不变量，不是兼容兜底，激进模式不动**：
- SessionLease 单写者 + rebind acquire-new-before-release-old（runtime.py:291-321）。
- fail-closed allowlist 咽喉点（router.py:78 FIRST check；children 永久 block `agent`，permissions.py:388；
  skill-hook shell 门控 engine.py:1002）+ 5 模式 PermissionEngine 语义 + protected-path + broken-extends→READ_ONLY
  （config.py:214-223）。任何替换必须**字节级行为等价**，安全回归测试不放松。
- cancel/abort 不变量：吞 CancelledError→`_aborted`、每个 poll 点、`_await_subagent_run` pending-set 超时判定。
- **每轮从树 rebuild 请求**（pi 是 turn 边界读一次 + turn 内内存 push，会漂移；nanocode 严格更强，docs/13 硬不变量）。
- render 层 inverse-orphan 清洗（render.py:132-136，pi 不做，删了会复现 abort-retry 的 provider-400）。
- capture-to-neutral parity（§3 step 0）。
- trajectory 派生自树三层边界 + `DURABLE_EVENT_FIELDS` additive 契约。
- prompt-cache 稳定前缀：volatile per-turn 注入必须 volatile_tail 置尾。

## 3. 落地序列（每步全测绿后即提交；D 系列为 destructive，严格按序）

| # | 步骤 | 类型 | 依赖 |
|---|---|---|---|
| 0 | **capture-at-emit parity**：AgentCore 构造 message-family 事件时跑 `capture.capture_*` 转 neutral（core 知道 provider 形状；`record_event._append_neutral`(session.py:113) 假设入参已 neutral）。helper + 单测先行 | 绿地 | — |
| 1 | **STEP D-1 message family 直接 cutover**：core.py 的 required `_tree_record`(:40/127/188/197/205/272/346/352/364/369) → `emit(UserMessageAccepted/AssistantMessageCompleted/ToolResultCompleted)` → `record_event` 唯一树写入者。**同一系列内删 flat fallback 四处 + append_to_last_user**（先确认无 treeless agent：子 agent 已有独立 lease，spawn.py:86-93）。emit 顺序必须 = 今日 inline 顺序（early-exec core.py:85-93 / openai batch :329-369），verify_turn_consistency 守 inverse-orphan。required raise 必须传播 | **cutover ★最高风险步** | 0 |
| 2 | **STEP D-2 telemetry + injection 事件化**：`_tree_event`/`_tree_custom_message`/`_dispatch_event` → `LlmRequestPrepared/ToolCallAuthorized/ToolBlocked/ContextInjected/AssistantDelta…`；单 emit sink 扇出 `[record_event, ui, recorder]`（杀 events.py docstring 点名的"双发"）；TURN_END 改 record_event(turn_completed) 单写，删 engine.chat:395 重复；**同一系列清除四处 silent except-pass**。遥测 emit 点（LLM_REQUEST/TURN_END/TOOL_BLOCKED/PERMISSION_DECISION/BUDGET_EXCEEDED）全量重发核对 | cutover | 1 |
| 3 | **AgentSession 升级为 turn shell**（= pi executeTurn + compact）：`run_turn` 拥有 lease prologue + `_loop_config()` + emit=record_event；engine 的 `_tree_*`/`_build_request_messages`/`_inject_*`/`_check_and_compact`/`_compact_conversation`/`clear_history`/`_auto_save` **迁入并删原方法**（不留 shim）；compaction 走 `CompactionRequested→record_event`；**同一系列删 plan_mode flat shim、`_context_cleared` flag、`_load_messages` 族 ownership shim、`_persist_state` resume 权威残余**。AgentCore.run_turn 签名收敛为 `(state, cfg: AgentLoopConfig, emit, *, stream_fn, signal) -> list[dict]`（cfg 注入 execute_tool=router.dispatch / authorize / persist_large_result / check_budget / rebuild_snapshot / is_aborted）。注意 flat `_{provider}_messages` 的删除前提：compaction（core.py:376-422）先改吃 `AgentState.project()` | cutover | 1,2 |
| 4 | **RuntimeThread events push 化**（EVENT-P2）：`subscribe(listener)→unsubscribe`，typed AgentEvent 词表（turn 边界/permission/compaction/abort 全覆盖），`{thread_id, session_id, seq}` 关联（不泄露 tree entry id，docs/12 boundary 5），rebind 发 session_switch 边界；`events()` 重实现为 push 流快照。listener dispatch try/except 包裹（fire-and-forget），ring buffer 防膨胀 | 绿地（可与 1-3 并行收尾后接） | 1,2 |
| 5 | **ToolSpec + ToolHost Protocol**：schema+executor 合一单 TOOLS dict（直接替换两份全局）；窄 Protocol `ToolHost`（诚实承认 ~19 成员）使 dispatch 依赖 typed port 而非 Agent；curated bundles（read_only_tools/coding_tools，pi index.ts:138-154 同位）。allowlist/permission 检查留在 chokepoint，绝不下推工具函数 | 绿地（可与 1-3 并行） | — |
| 6 | **STEP E 上下文 cutover**：date/git 移出 system prompt（**实际 live bug**：过午夜/commit 后 stale），走 per-turn ContextRuntime collect（EnvProvider/GitSnapshotProvider 已建在 providers.py:68/82，engine.py:423 硬编码 False 翻开改走新 per-turn 路径）；四个手写注入器 provider 化（SkillListing=until_compact / SkillBody=one-shot / FinishedTasks=turn / MemoryRecall=turn），ContextLedger 拿到全量 /context 可见性。collect 保持纯（产 packs 不写树）；各注入器 dedup/ordering 不变量保留：skill_listing 写成功后才前进 dedup（engine.py:861-864）、finished_tasks pin LIVE leaf 非 spawn branch（:925-927）、memory 成功后才更新 `_already_surfaced`（core.py:73-75）；git subprocess per-turn 缓存 | 半 cutover（逐源） | 3 |
| 7 | **AgentProfile spawn cutover + config.py 退役**：spawn 用 `derive_child_profile/effective_child_tools`（permissions.py:35-69，已测但 dead）；`_resolve_effective`+trust gate port 进 registry；ResultEnvelope typed cutover（spawn.py:248-265）保 4KB bound；PermissionEngine→PermissionContext 同系列替换。fail-closed 语义字节级等价 | cutover | 5 |
| 8 | **output-cap 补齐**：run_shell **tail-keep**（现全局 50KB head+tail 会裁掉失败命令结尾，pi truncateTail 刻意保尾）；grep per-line ~500 字符 + 统一 100-match；read byte cap 对齐或文档化。一律不绕过 persist_large_result(30KB spill) | 绿地 | 5（可独立） |
| 9 | **chain / parallel fan-in host 原语**：借 pi `{previous}`（index.ts:530）与 fan-in 的**编排点子**，做成审计过的 host 原语（每步独立 leased child session + bounded ResultEnvelope），不耦合仍是骨架的 TeamRuntime；team_* entry 保持 non-FOLD/non-leaf（tree.py:131） | 绿地 | 7 |
| 10 | **compaction 触发健壮化**（pi 五门的有用子集）：overflow-error 恢复（今天 overflow=死 turn）、abort 门控、keepRecentTokens 预算 cut-point——cut-point 与 summarizer kept-suffix 必须同步改，否则两区 fold 双计 | 增强 | 3 |

**最高风险步 = #1**：同时承载 (a) 唯一写入者切换（激进模式下无双写过渡，错了就是当场红测，不是静默漂移——这正是激进的优点）、
(b) capture parity、(c) emit 顺序 = inverse-orphan 一致性、(d) required raise 传播。#0 的单测 + 四路径专项测试是它的全部前置。

## 4. 明确不抄 pi 的清单（nanocode 更强处，全部保留）

| 项 | 为什么不抄 |
|---|---|
| SessionLease 单写者 | pi 无 lease/单写强制；nanocode 共享 worktree 多进程必需 |
| fail-closed allowlist + 5 模式 PermissionEngine + 危险命令 + protected-path | pi **没有** per-call 权限门（README 自承 allowlist "not perfectly enforceable"）；抄 pi = 删安全模型 |
| first-class 父子 session（manager.py:116/235/311 lineage + resumable） | pi 仍是 `--no-session` ephemeral 子进程；nanocode 全面更强 |
| full-length 单调 id（tree.py:21） | pi 8 字符截断 + 碰撞重试是其截断才需要的补丁 |
| 每轮从树 rebuild 请求 | pi turn 内内存 push 会漂移 |
| render 层 inverse-orphan 清洗（render.py:132-136） | pi 靠 provider 拒绝；删了复现 abort-retry 400 |
| ContextLedger | pi 没有，context 质量不可 debug |
| trajectory 三层边界 | pi 无此层（docs/10） |
| pi 自身的债 | 双套 session/compaction、三个冗余事件消费面、string-channel EventBus、无沙箱 jiti 扩展加载、双构造路径——全不抄 |

## 5. 推迟项（非本轮）

- rpc-mode / 真·custom tools（`extra_tools: list[ToolSpec]` 注册进 CapabilityRouter REAL 分支）：等第二个客户端 +
  RUNTIME-P0 bootstrap 内化（cli.py:724-835）之后。host 工具拦截 hook 若做，必须坐在 allowlist+PermissionEngine 之后，能收紧不能绕过。
- repo map tree-sitter 升级、TeamRuntime 调度闭环：green-field，与本轮 cutover 解耦。

## 6. 验证基线

- python 入口：`uv run`（uv.lock 已入仓）或 `.venv/bin/python`；**勿用裸 python3**（无项目依赖）。
- 共享 worktree 多 session 并发：按 targeted per-module 子集验证（tests/agent, tests/session, tests/context,
  tests/subagents, tests/capabilities, tests/runtime），不做全量盲归因。
- 每个 cutover 步骤的守门测试：verify_turn_consistency 五条断言、subagent security regression / callgate / caps 全绿不放松。

## 7. 附录 A — 平台愿景对账单（2026-06-12 三轮源码审计）

愿景：「外部像 Codex 可协议化/可嵌入/可服务化，内部像 Pi 事件化/树状会话化/可 fork/replay/审计；
YAML skills + 工具级 hooks + 统一 PermissionEngine + Sandbox + 持久化 subagent 与后台任务；
Pi 式 session tree 唯一事实源、state 可丢弃投影；CC 上下文工程 + Aider repo map；激进删兼容；多 agent 预留空间。」

| 条款 | 状态 | 要点 |
|---|---|---|
| session tree 唯一事实源 | ✅ 做到（全仓质量最高） | 每轮 render(build_context()) 重建、无 flat 权威、缺 lease fatal；resume 纯树；等价测试含落盘重开；compaction 追加式 |
| state = 可丢弃投影 | ✅ 做到 | flat 列表 turn-local 每轮覆盖；hydrate_state 从树重建；v2 state.json 仅 derived cache |
| fork / replay / 审计 | ✅ 做到 | /fork /checkout /rewind /tree /clone live+测试；遥测 entry 在树内 non-fold；trajectory derived-only 且边界 **CI 强制**（AST+子进程测试）。名义差：`trace` 命令有意 retire（b5778a2），改 /tree + `nanocode trajectory` |
| 内部事件化（Pi 式） | ❌ 未做 | AgentEvent 零构造、record_event 零调用、core 171 次 host 触达 → 本文档 #0-#3 |
| 可嵌入（in-process） | ✅ 做到 | AgentRuntime/RuntimeThread/AgentConfig/ApprovalManager 公开导出+测试；runtime 唯一 turn 路径（逃生阀已删） |
| 可协议化/服务化 | ⏸ 有意未做 | app-server/JSON-RPC 零代码 = docs/09 自定门槛（等第二个 client）。中间欠账：events() PULL 快照无外部消费者；approvals 身份在 message 字符串；bootstrap 在 cli.main 且 REPL 绕过 thread_start |
| YAML skills | ✅ 做到（最完整轴之一） | CC 式渐进披露全链路 live（stub→预算 listing delta→调用后注入 body→paths 激活→fork 收窄），38 测试。缺口见附录 B-③④ |
| 工具级 hooks | ◐ 部分 | pre/post-tool-use 包裹每个 REAL 工具，pre 可 block；hook shell 是三大 fail-closed 点之一（强制 native-sandbox-or-blocked）。缺：仅 skill frontmatter 配置面、无 session 事件、不可改写 input/output、HookPolicy.allow_skill_hooks 死字段 |
| 统一 PermissionEngine | ✅ 做到 | 5 模式+危险命令+protected-path 先于 bypass+escalate，单咽喉点；MCP 也在 allowlist 下。PermissionContext 平行层 designed-only → #7 收编 |
| Sandbox | ✅ 做到 | seatbelt/bwrap/microVM 已在 main live（统一 plan_shell、off-PATH 防劫持、fail-closed），4 轮 Codex 加固。缺口：**默认 OFF**（NANOCODE_SHELL_SANDBOX opt-in，PR-6 未翻） |
| 持久化 subagent + 后台任务 | ◐ 大部分 | 后台 spawn（并发上限/四终态/cancel-swallow-safe 超时判别）+ live-leaf 提醒 + 双面板 = 完整；child session 一等公民（lineage、/resume /parent /agent）= 完整。名实差：persistent = terminal 续跑，in-flight 不可 reattach（重启标 lost）；typed ResultEnvelope designed-only（live 用 dict，4KB bound 真实） |
| CC 上下文工程 | ◐ 部分 | ContextRuntime/packs/budgets/Ledger 建好，live 仅 2/~10 源；date/git stale 是 live bug → #6 |
| Aider repo map | ◔ 偏低 | 词法 + /context 展示 only；无 tree-sitter/PageRank、不在请求路径（§5 推迟项） |
| 激进删兼容 | ◐ 部分 | 已删：snip/microcompact、快照权威、wire-authority、Tracer、逃生阀。未删：flat fallback×4、config shim、silent except×4、plan_mode shim → 本文档全接管 |
| 多 agent 预留空间 | ✅ 预留做到 | TeamRuntime 骨架 + team_* entry（non-FOLD 不变量：协作永不漏进 LLM context）+ AgentProfile 全建好；接线未做 → #9 |

**超出参照系的六样**（Pi/CC 均无，保持）：SessionLease 单写者、call-time fail-closed allowlist、ContextLedger、
trajectory CI 强制边界、一等公民 child session、microVM 隔离上限。

**短板同根**：事件脊柱 / AgentProfile / PermissionContext / ResultEnvelope / HookPolicy / McpServerRef 全是
「typed 层建好+测试通过+live 未切」——补法收敛于本文档 #1/#2/#3/#7/#9 五步，非发散修补。

## 8. 附录 B — 审计新发现小修清单（并入排序，不重编主表）

| 编号 | 项 | 修法 | 建议时机 |
|---|---|---|---|
| B-① | MCP `disconnect_all` 零调用（子进程泄漏） | session/CLI 退出路径调用 disconnect_all（连同 lazy-init 生命周期对称化） | 独立小修，随时 |
| B-② | 根 `.mcp.json` 被 ships 但 `_load_configs` 不读；docs/07 仍是 `.claude` 路径 | `_load_configs` 增读根 `.mcp.json` 或删示例文件统一到 `.nanocode/mcp.json`；docs/07 更新 | 独立小修，随时 |
| B-③ | inline skills 的 `allowed-tools` 是 no-op（仅 fork 分支收窄，engine.py:1129 唯一消费点） | inline 路径在 skill 生效期间做 allowlist 交集，走既有 fail-closed allowlist 机制（不得新开旁路） | 随 #5 ToolSpec 或 #7 一起做（同属工具门控域） |
| B-④ | `user_invocable:false` 仅列表隐藏，模型点名仍可调（_execute_skill_tool 只查 disable_model_invocation） | 语义决策后在 _execute_skill_tool 边界强制（两个 flag 的正交语义对齐 CC） | 独立小修，随时 |
| B-⑤ | `/fork` 帮助文案 stale（写"into a new session"，实际 in-file 移 leaf） | 改 builtin.py:555 文案 | 顺手 |
| B-⑥ | sandbox 默认 OFF（PR-6 翻转未做）——「默认安全」停在权限层未到隔离层 | **独立决策项**：native backend 可用时默认 `auto`，不可用 fail-open 保持现状并提示 | 用户决策后单独落 |
| B-⑦ | resolve.build_skill_descriptions 死代码（被 listing.py 取代） | 删除 | 顺手 |

## 9. 附录 C — 老旧兼容与无效兜底狩猎清单（2026-06-12，6 镜头 × 75 候选 × 逐项对抗校验）

前提（用户定调）：**项目无用户、无适配期**——任何「为兼容保留」「容忍不可能状态的兜底」「旧格式读写器」一律删。
校验口径：每项过一轮「反驳删除」对抗校验；§2 列的安全/正确性不变量自动 keep。

### C-1 立即删（零生产调用 / 纯转发 / 死防御，无步骤依赖，~20 项）

| 项 | 位置 | 说明 |
|---|---|---|
| print_assistant_text | ui.py:48-50 | 注释自证 "kept for compatibility (legacy raw path)"，全仓零调用（`import sys` 保留，spinner 在用） |
| _trace_host_dir / _sandbox_name 无参兼容版 | sandbox_shell.py:217-220, 279-282 | 被 `_for(p)` 显式注入版取代；连带删 pinning 测试 test_legacy_sandbox_name_still_env |
| save_session（flat 快照写者） | session/store.py:12-13 | engine.py:689 自证「现冗余」；零运行时调用（读者面归 C-3） |
| write_main_messages / read_agent_messages | session/v2.py:42-43, 80-81 | 写者无生产调用；读者仅测试调用（子 agent 历史从 child tree 重载） |
| persist_agent_messages back-compat artifact | runtime/spawn.py:106-117 | 写 agents/<id>/messages.json 无人读（其读者 read_agent_messages 已死）；resume 从 child tree 重载 |
| engine re-export shim | engine.py:87-89 | SubAgentRunner/_auto_deny_confirm 旧 import 路径转发；测试改直接 import runtime.spawn |
| cli re-export shim | cli.py:36, 354-355 | handle_eval_command/_fmt_eval_row 仅为测试旧 import 路径；测试改 import commands.builtin |
| 六个委托 wrapper 方法 | engine.py:1057-1074 | _running_background_subagent_count 等纯转发 spawn；调用方/测试直连 |
| _replace_messages / _append_message | engine.py:569-573 | 纯 passthrough + 零调用死方法 |
| {{claude_md}}/{{memory}} 空管道 | prompt.py:12, 153-154, 170-171 + system_prompt.md:78-79 | Phase 3 cutover 后恒空的占位符 + unused import build_memory_prompt_section |
| _session_mgr None 防御（main agent） | engine.py:415-416, 446-448 | 收紧：保留 is_sub_agent 早退，删 `_session_mgr is None` 半边（lease 保证非 None，缺即 fatal） |
| SIGINT 死分支 | cli.py:508-517 | 硬编码 agent_capturing_output=False 后的不可达分支 |
| SkillDefinition getattr 防御 | skills/listing.py:44-46 + engine.py:1112 + cli.py:604 | dataclass 字段恒存在，改直接属性访问 |
| TrajEvent 双形态容忍 | trajectory/project.py:517-554 + eval.py:30-32,75 + _tree_events.py:51 | getattr 访问器块 + SessionEvent 别名 + 永假的 legacy 字段（TrajEvent 恒为 dataclass） |
| 旧 wire 时代残留 | trajectory/metrics.py:47-50, 247-251, 420-434 + _tree_events.py:366-369 | legacy 'Z' 时间戳回退、旧 wire ts-diff 回退、Step/dict 双形态、stale wire-shape docstring（wire 已退役 b5778a2） |
| stale 文案/注释 | builtin.py:438-440（/resume 自称会自动迁移，假）、v2.py:1-2 docstring、test_p5_injection_entries.py:42 注释 | 改文案，不动代码 |

### C-2 随 docs/16 步骤删（22 项归并，按步骤分组）

- **随 #1（事件反转）**：tests/agent/test_cutover_characterization.py 整文件（roadmap:165 早已判死，钉的就是
  append_to_last_user + 三个 _inject_ flat）；tests/skills/test_listing.py:46-67 四个 append_to_last_user 测试；
  test_memory_consolidate.py:195-212 flat 注入断言。
- **随 #3（turn shell）**：engine.clear_history else flat 分支(:513-517)；_persist_state 无读者键
  session_id/startTime(:780-782)；tests/agent/test_resume_lost.py 整文件（钉 _persist_state/_reload_task_state 作
  resume 权威）；test_rebind_session.py 的 _context_cleared 工作集项；sandbox_shell._session_id_of 的 env 回退(:149-155)。
- **随 #4（events push）**：runtime_events.py 的 DURABLE_TYPES/DURABLE_EVENT_FIELDS/EPHEMERAL_UI_TYPES/EventDispatcher
  ——wire 时代残留，但 trajectory additive 契约迁移完成前不可动。
- **随 #6（context cutover）**：tests/agent/test_skill_injection.py flat 钉点、test_background_shell.py:76-112 五个
  flat 注入器测试、test_background_subagent.py:201-219、test_full_p6b_review.py 的 flat 驱动用例；
  memory/eval_store.py:113-118 v2 state.json 溯源回退。
- **随 #7（config.py 退役）**：tests/subagents/test_config.py / test_config_extended.py / test_curator_config.py /
  test_eval_curator_config.py 整面改写为 build_profile 断言；**test_subagent_security_regression.py 的 4 处
  get_sub_agent_config 调用 = 迁移不删除**（fail-closed 代数断言字节级等价，同 commit 改指向）。

### C-3 整面决策删除 — legacy 会话导入面（用户前提已成立 → 建议整批删）

校验中 10 个「keep」判定全部依赖同一前提：「盘上可能有 pre-docs/14 旧会话，删了会丢失导入能力」。
用户已明确：无用户、无适配期。唯一活体调用者（/resume 的 legacy 列表）本身在删除候选内。
**按调用者先行顺序一批删**：

1. builtin.py /resume 的 legacy 部分：`*.json` glob 候选(:457-462,473-476)、inspect_session import(:443)、`legacy=N` 列(:488)。
2. sessions_cmd.py 整文件 + cli.py:39 `sessions` 子命令注册 + cli.py:729-740 legacy 守卫与 migrate 提示
   （runtime.py:335 / engine.py:582 注释同步改）。
3. session/migration.py 整模块。
4. session/store.py：load_session / list_sessions / get_latest_session_id 的 legacy flat + v2 回退分支
   （只留 session.jsonl header 扫描）。
5. session/v2.py：read_main_messages（state.json 读写 + agent artifact 写者仍 live，保留）。
6. 测试连删：test_p7_migration.py、test_v2.py legacy 用例、test_resume_adopt.py legacy-only 用例、test_session.py flat 断言。

**后果（一句话）**：盘上 pre-break 的 flat/v2 旧会话从此不可发现、不可导入（文件不删，只是失去工具）。

### C-4 真 keep（对抗校验维持保留，勿再报）

- memory/store.py:55-60 `memory_type` 嵌套/扁平双读：**活的读者契约**（memory 文件可手写/导入，宽于写者），3 个专项测试锁定——非兼容层。
- subagents/config.py:54,65,68 `.agents/agents` 目录发现：README:36/224 在售的 **vendor-neutral 特性**（注释自称「通用约定」），且在 P4 trust gate 之内——是功能不是兼容。若要砍属产品决策，且需 #7 先落地。
- test_subagent_security_regression.py 的 get_sub_agent_config 调用面：迁移不删除（见 C-2 #7）。

