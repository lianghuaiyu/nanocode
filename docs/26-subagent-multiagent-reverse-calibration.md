nanocode subagent / multi-agent 架构反向校准与多方案改造设计
调研基线：nanocode 7b34f42（branch cursor/2faad539），逐文件读源码。同行系统按下文“证据状态”分级。本轮不改代码。

---

## 0. v2 校准更新（2026-06-27，rebaseline 到 main `fb088ba` + 三仓逐行源码审计）

> 本节是阅读全文的入口。§1–§13 的调研基线是 `7b34f42`（2026-06-25，branch cursor/2faad539），**已落后 main 10 个提交**。在本文档落笔之前，`8629640`「Phase A 清债 (docs/25 §4)」（2026-06-26）已经修复了 §3 的 **D1 / D2 / D5 / D8①** 四项。因此 §3 缺陷清单、§9 推荐、§11 阶段 0 的「必修」项里**有相当一部分是已完成的工作**——照旧执行会重做 Phase A。本节按 main 现状重新定级，并补入对 Pi / Codex / OpenCode 三仓**逐行源码审计**的结果（多处证据从 docs-derived 升级为 Verified-from-source，并修正了若干此前的误述）。

### 0.1 §3 缺陷清单——按 main(`fb088ba`) 现状重新定级

| 缺陷 | 状态 | 依据（main 现状） |
|---|---|---|
| **D1** reserved-agent 断裂 | ✅ **已修**(Phase A A1) | 改走 `new_child_session_id + begin/finish_run_record` 四态对称；`create_subagent` 在 src/ 已为零；`tests/subagents/test_reserved_agent_run_record.py` 覆盖。`spawn.py:1359-1428` |
| **D2** 双账本 | ✅ **已修**(Phase A A2) | 收敛成单账本(run_record)；注入迁入 run_record(`injectSummary`/`mark_injected`)，`FinishedTasksProvider` 双源化；`write_host_task_result` 已死代码。`spawn.py:1085-1130`,`context/providers.py:335-381` |
| **D5** wake 半契约 | ✅ **已修**(Phase A A3) | wake 8 处 + `wakeRequested` 全删；收敛成 steer(live)/resume(terminal) 对称两态；`tests/subagents/test_no_wake_schema.py` |
| **D8①** 重绘写 lost | ✅ **已修**(Phase A A4a) | `view_for_parent` 内存算 lost、不落盘不发事件；`mark_lost` 只在显式生命周期。`runs/ledger.py:58-81` |
| **D8②** 派生索引 cache | 🔴 **未做**(A4b 按 gate 推迟) | `children()→_scan_headers()` 仍每帧全盘 O(N)(`manager.py:349-376`)。**Phase A 注明「待实测确认瓶颈」——应先计测再决定是否上索引，避免过早优化。** |
| **D3** 后台确认无法升级回父 | 🔴 **未做** | `_auto_deny_confirm` 仍是后台子 confirm_fn。`spawn.py:86-88,226` |
| **D4** 重启即 lost | 🔴 **未做** | 无 LiveThread/journal；在飞子 resume/重启即 lost。`ledger.py:40-56`,`spawn.py:650-652` |
| **D6** chain/parallel 占满父 turn | 🔴 **未做** | 同步跑完、拼接字符串；`run_in_background` 被显式禁止。`spawn.py:834-909`,`601-602` |
| **D7** TeamRuntime 死骨架 | 🔴 **未做** | `runtime/teams.py` 纯内存、除 lazy re-export 外无调用方；`TEAM_*` 从未落 session.jsonl |
| **D9** 子 trajectory 不分叉 | 🟡 **设计取舍(非缺陷)** | `spawn.py:238`，代码自注明 trajectory 不分叉。**需拍板，不是待修 bug**(理念 #4 说「继承」与「可独立评估」张力) |
| **C2** 血缘未正交 | 🔴 **未做** | `parentSession` 单 dict 混 `{sessionId,entryId,taskId,agentId,forkedBeforeEntryId}` |
| **D10/D11** | ⚪ **观察项(非缺陷)** | 文档自评「可接受的设计」 |

**一句话：阶段 0（方案 A）已由 Phase A 落地；当前真实未完成 = D8②(待计测) + D3/D4/D6/D7/C2 + D9(待决策)。**

### 0.2 三仓源码审计——修正此前的 docs-derived 误述

逐行读了本地参考仓 `pi@2417adb` / `openai-codex@566f7bf` / `opencode@9dadc24`，以下此前（§4/§5/记忆）的 docs-derived 说法需更正：

| 此前说法 | 源码实况（更正） |
|---|---|
| OpenCode 有「独立 `task_status` 工具」(C12/§4 表) | **错**。`task_status.ts` 不存在，且被显式不注册(`test/tool/registry.test.ts:69-75`)；状态走 SSE 事件，无 poll 工具。 |
| OpenCode 子 session「嵌套/隐藏」(§4 表 Vis/List) | **半错**。子不是 create 时自动隐藏，而是 listing 传 `roots`/`parent_id IS NULL` 的**查询过滤**(`session.ts:1022-1024`)——是查询契约不是不变量。 |
| Codex「LiveThread 让在飞会话可 resume 重连」(C6/D4) | **需加硬限定**。Codex **不跨进程恢复在飞 turn**：liveness 是进程内 `HashMap<ThreadId,Arc<CodexThread>>`，进程死即丢；冷 resume 注入 `TurnAborted{Interrupted}` 边界、不重放孤儿 turn(`thread_manager.rs:1734-1790`)。「重连」=同进程 reattach，「resume」=重放历史+中断标记。 |
| Codex「agents.max_depth 默认 1」(§6) | **证实但有坑**：默认 1(`config/mod.rs:260`)，**只在 V1 工具层强制，V2 忽略**(测试 `multi_agent_v2_spawn_agent_ignores_configured_max_depth`)。nanocode 在内核 fail-closed 限深更强。 |
| Pi `_bindExtensionCore`/`_refreshToolRegistry` 在 `packages/agent/src/harness` | **错**。在 `packages/coding-agent/src/core/agent-session.ts`。harness 包是内核(loop+注入契约)，`coding-agent` 才是装配扩展/工具/模型的宿主层。 |
| Pi core 内置 subagent | 本文已对(core 无)，补强：唯一 subagent 是 example extension，shell 出独立 `pi` 进程(`examples/extensions/subagent/index.ts`)；**core 无 spawn 原语、无 depth cap、无子权限派生**——nanocode 内核 envelope 在这三点更强。 |
| Codex `parent_thread_id ⟂ forked_from_id`(C2) | **证实，升级为 Verified**：`responses_metadata.rs:142-143` 两字段独立、四组合全可达；delegate 同时设两者但来源独立(`codex_delegate.rs:103-104`)。 |

### 0.3 三仓源码审计——新的优化机会（源码级蓝图）

每条都给出「源码出处 → nanocode 落法」，作为 docs/25 后续阶段的实现依据：

- **O1（D3 修复蓝图｜Codex，替换原 C1 的 Claude-Code-闭源依据）**：approval = `oneshot` + turn 上 `pending_approvals: HashMap<approval_id, Sender<Decision>>`，turn 阻塞在 `rx.await`(`session/mod.rs:2090-2162`)。子的 approval 升级回父：`codex_delegate.rs:handle_exec_approval(457-539)` 把子的 `ExecApprovalRequestEvent` 用 `parent_session.request_command_approval`(511-523) 重新发起（或路由给 guardian reviewer），决定经 `Op::ExecApproval` 回灌子。**nanocode 落法**：run_record 增 `blocked(pending_approval)` 态；后台子 confirm_fn 不再 auto-deny，而是挂到父 session 的 pending_approval 队列 + emit 升级事件；父在 /agents 应答 allow/deny(单次)，决定经现有 steer/send 通道回灌；可选 guardian = 扩展钩子(层④自动裁决)。

- **O2（D6 修复蓝图｜OpenCode，比原方案 B 更干净）**：每个子（含前台）都经 job-registry 句柄跑：`done` deferred + `promote` deferred。前台 = `race(wait, waitForPromotion)`(`task.ts:303-321`)；`promote(id)` 把阻塞 await **原地转后台、不重启子**(`background-job.ts:302-334`)；完成经**合成消息注入**重入父 turn(`inject` 把 `<task>` envelope 作 synthetic user-prompt fork 进父，`task.ts:202-240`)。**收益**：①后台化不改 tool 返回契约 ②增量可见(完成自动重入父) ③配合 cancel 级联。**nanocode 落法**：把 chain/parallel 抽到编排扩展(层④)，每步经 host 的 run handle(层③)；`run_in_background` 不再禁止而是 promote；完成回注复用 Phase A 已打通的 `FinishedTasksProvider` 注入。

- **O3（cancel 级联蓝图｜OpenCode）**：取消 = 对 job registry 按 `{sessionId, parentSessionId}` 元数据做**不动点扫描**(`run-state.ts:116-148`)，非 live 对象树遍历，天然到任意深度。**nanocode 落法**：run_record 已有 parent 元数据，`cancel(orchestration)` 用同样 fixpoint 扫 `_nanocode_run_id` 族。

- **O4（D4 修复蓝图｜Codex，含现实上限）**：持久/在飞两平面分离——`ThreadStore` trait(可换 local/remote) + `LiveThread` 句柄；resume **先查 live registry(进程内 HashMap) 再落盘**，在飞则 reattach、否则冷 resume；`ThreadStateManager` 订阅多路复用(Weak listener + generation 计数防陈旧 reattach)。**现实上限**：不跨进程恢复在飞 turn，冷 resume 写 `TurnAborted{Interrupted}` 边界。**nanocode 落法**：现 `_background_tasks` 即「进程内 live registry」雏形，让 agent resume 先查它(在飞→reattach 而非拒绝)，跨进程则冷 resume + 写中断 entry。**不追求** journaled 重放已完成 agent（@quintinshaw 原型级语义，与 canonical session 一致性冲突）。

- **O5（受信 spawn 槽蓝图｜三仓共识，本设计的命门）**：三家殊途同归——扩展永远拿不到「给子裸配工具」的权力，子的工具/sandbox 由内核派生并对上限校验。Pi：curated 闭包内 gated action(`setModel` 无 auth 返回 false, `agent-session.ts:2248`)；OpenCode：plugin 只拿 HTTP client，session 影响必经权限门 API，子权限 = 父 deny ∪ 失败闭合 `task`/`todo` deny(`subagent-permissions.ts:14-27`)；Codex：子策略 copy 父 effective → 叠 role → 重钉 runtime 不变量 → `Constrained<T>` 允许集校验，子不可超 ceiling(`constraint.rs:162`)。**nanocode 落法**：层④扩展只能调 host 的 `runtime.spawn_subagent(profile, prompt, ctx_mode, isolation, background)`；`effective_child_tools + sandbox_profile` 由内核派生(已存在)，扩展不得传裸 caps；内核做「子≤父」ceiling 校验(已有 allow∩/deny∪/剔 agent)。**= 把现有派生逻辑暴露成 curated 闭包槽，而非新建机制。**

- **O6（lifecycle 事件契约蓝图｜Pi；并指出可超越点）**：Pi 双层事件——内核发穷尽 union(`harness/types.ts:634-656`)，扩展面是另一套 curated 重载(`extensions/types.ts:1125-1163`)，且**带 typed event→result map**，拦截型事件(`tool_call→{block,reason}` / `session_before_compact→{cancel,compaction}`)让扩展能塑形不能绕过。**OpenCode/Codex 都没有专门的 `subagent.*` 事件**（子靠 parentID 在通用 `session.*`/`message.*` 流里区分）——这是 nanocode **可做得更好**的点：把 `SubAgentStarted/Completed/Failed/Steered/BlockedPendingApproval` 暴露为**稳定 typed 扩展事件**(C9/C11)，承接 before_compact 钩子的「typed in/out」诉求。

- **O7（工具注册冲突防御｜Pi 反面 + Codex 正面）**：Pi registry 是 last-write-wins，扩展能 shadow `read`/`bash`/`edit`(`agent-session.ts:2326-2364`)——**反面教材**。Codex built-ins 永远赢冲突(`spec_plan.rs:1019`)、exposure 与 dispatch 解耦(`ToolExposure::{Direct,Deferred,Hidden}`)。**nanocode 落法**：扩展工具与内置同名时拒绝或命名空间化；保留 provenance 标签；内置不可被扩展覆盖。

- **O8（swarm 轻量化｜OpenCode + Codex V2）**：对等协作不必一上来就 board/claim 全套——OpenCode 的「元数据驱动 cancel 不动点」+ Codex V2 的「inter-agent mailbox/send_message」表明**有界 mailbox + 元数据**就能撑起取消/通信，比 §7 方案 D 的持久 board 轻。但**共享可变态(mailbox/board)的持久化仍须下沉层③**(session-tree 单写者权威)，策略才在层④。

### 0.4 分层编排架构（本次落定的目标形态）

手机比喻 2.0 四层映射，把方向 A（multi-agent）切在「原语 / 策略」缝上：

```
④ 扩展     编排「策略」: chain/parallel · planner-worker · reviewer-loop · swarm · acceptance · 动态 fanout   ← 可装可卸 (默认 UNTRUSTED)
─────────────────────────────────────────────────────────────────────────────────────────────────────
③ 宿主服务  spawn_subagent 原语 · run_record · 权限/sandbox 派生 · lifecycle typed 事件 · 持久/在飞 registry   ← 靠注入换实现
②  内核     bounded envelope · effective_child_tools · depth cap(fail-closed) · 单写者锁 · ceiling 校验          ← 焊死,扛安全不变量
①  模型     —
```

- **B（单 agent coding 质量）不是一层**，是 ①②③ 协同的结果；**子 agent 跑同一内核 → 改 B 只会让 A 免费受益**，二者不抢层。要避免的不是「A、B 抢层」，而是「编排策略焊进内核循环」（现状 `execute_agent_chain/parallel` 长在 `spawn.py` 即层错位，阶段 1 应上提层④）。
- **命门（O5）**：扩展默认 UNTRUSTED(`facade.py:268`)，编排在层④但要 spawn 子，必须经**受信 spawn 槽**——子工具/sandbox 由内核派生，扩展不得传裸 caps，否则即提权。
- **depth cap 留内核**（Codex V2 在工具层限深被绕过是反例）；**swarm 的共享态持久化下沉层③**（O8）。

### 0.5 修订推荐路线：A→C，并与 docs/25 对账

- **与 docs/25 的关系**：docs/25 是「Route A→C subagent orchestration landing plan」（落地计划正本），Phase A 是其 §4 的执行；docs/26 是「反向校准 + 源码审计依据」。二者**同向对齐**：本文档 v2 的 §0.3-§0.4 为 docs/25 后续阶段补充源码级蓝图。两份并存、不冲突。
- **推荐从原 §9 的 `A→B+C1+C2` 修订为 `A→C`（分层编排基底）**。理由：用户确定走方向 A 且要求模块化 / 嵌入式边界；`chain/parallel` 现长在内核层属层错位，应上提层④；D6 用 O2(OpenCode job-handle) 在扩展层实现，D3 用 O1(Codex approval) 在内核原语侧补 blocked 态，C2 用 Codex 正交字段，D4 用 O4(含现实上限) 按需推进。

### 0.6 修订后的分阶段计划

- **阶段 0 ✅ 已完成(Phase A)**：D1/D2/D5/D8①。剩 **D8②** 待计测确认瓶颈后再定夺（默认不做）。
- **阶段 1（层④编排基底 + D6 后台化）**：抽 `chain/parallel` 到 first-party 编排扩展；内核稳定 `spawn_subagent` + lifecycle typed 事件(O6) + 受信 spawn 槽(O5)；run handle + promote + 合成回注(O2) + cancel 不动点级联(O3)。
- **阶段 2（D3 确认升级 + C2 血缘正交）**：run_record `blocked/pending_approval` 态 + 父侧应答 + guardian 扩展钩子(O1)；`parentSession` 拆 `spawned_by ⟂ forked_from`(Codex 模型)。
- **阶段 3（按需）**：D4 持久/在飞分离(O4，认现实上限)；acceptance gates / output schema / 动态 fanout；swarm 轻量化(O8)。

---

1. 一句话结论
nanocode 的 subagent 底盘其实比大多数 Pi 扩展更干净（child session 真·first-class、bounded envelope、typed 权限派生、worktree），但它在**“长活后台执行 + 子→父确认升级 + 多 agent 协作”三处明确落后于 Codex / Claude Code / pi-subagents**，且自身带有已断裂的 reserved-agent 路径、双账本（run_record vs TaskManager）、TeamRuntime 死骨架、O(N) 全盘扫描等实打实的债务——主推方向：先修契约债 + 借 Claude Code v2.1.186 的“后台子 agent 把确认升级回父会话”，再借 Codex 的 parent_thread_id ⟂ forked_from_id 正交建模和 pi-subagents 的 acceptance/intercom，最值得对标的是 Codex（工程化）与 pi-subagents（功能面）。

2. 调研范围和证据状态
系统	repo / source	commit / version	读源码	关键文件	证据可信度
nanocode
本仓库
7b34f42
是（逐行）
runtime/spawn.py,subagents/run_record.py,runs/*,session/manager.py,runtime/facade.py,tui/session_pages/agents.py,agents/*,runtime/teams.py
Verified from source
OpenCode
sst/opencode (anomalyco fork mirror)
dev；bg-subagents 22de34c(#27084)、f4306d5(#24174)
是（task.ts schema + commit diff + session 模型）
packages/opencode/src/tool/task.ts,tool/task_status.ts,session/session.ts,session/run-state.ts
Verified from source（核心）/ docs-derived（TUI listing）
Pi core
earendil-works/pi(=badlogic/pi-mono)
main,v0.74.1
是（docs+SDK+runtime ts 摘录）
docs/extensions.md,docs/sdk.md,core/agent-session-runtime.ts,core/extensions/index.ts
Verified from source（API 面）
Pi official examples
earendil-works/pi examples/sdk/*
main
是（13-session-runtime.ts）
examples/sdk/13-session-runtime.ts
Verified from source
pi-subagents (nicobailon)
nicobailon/pi-subagents
v0.28/0.29,2210★,90.8K/mo
README/源码结构（file-by-file 说明，未逐 .ts 行读）
src/runs/foreground/*,src/runs/background/*,src/runs/shared/worktree.ts,src/intercom/*
Verified（README/结构级） / inferred（实现细节）
@tintinweb/pi-subagents
npm @tintinweb/pi-subagents
0.10.3,22.2K/mo
catalog + 第三方对比文档
—
Docs-derived
@gotgenes/pi-subagents
gotgenes/pi-packages
16.x,21.3K/mo
决策文档+package 页
docs/decisions/0002-*.md,docs/comparison-with-upstream.md
Verified（docs/ADR 级）
@quintinshaw/pi-dynamic-workflows
QuintinShaw/pi-dynamic-workflows
commit b781fca
README+commit diff
src/worktree.ts,src/workflow.ts,src/agent.ts
Verified（README/commit 级）
michaelliv/pi-dynamic-workflows
michaelliv/pi-dynamic-workflows
main
README/结构
src/workflow.ts,src/agent.ts
Docs-derived
pi-sub-agent
pi.dev/packages/pi-sub-agent
—
catalog
extensions/index.ts
Docs-derived
@e9n/pi-subagent (espennilsen)
espennilsen/pi extensions/pi-subagent
main
README/结构
—
Docs-derived
pi-intercom
npm pi-intercom
—
经 pi-subagents README
—
Docs-derived
Codex
openai/codex (codex-rs/)
main,eaf81d3f；PR#25113、f1923a3(#18882)
app-server README + commit diff + dev docs
app-server/README.md,core/(ThreadStore/LiveThread),sandboxing/
Verified（协议/commit 级） / docs-derived（subagent 用法）
Claude Code
code.claude.com/docs
2026-06 docs（含 v2.1.186）
官方文档
sub-agents,tools-reference,hooks,agent-sdk/subagents
Docs-derived（闭源）
3. nanocode 当前缺陷清单（不客气版）
每条都带源码定位。结论分级见 §13。

D1. reserved-agent 路径已断裂（Verified from source，高危）　→【v2(§0.1)：✅ 已由 Phase A `8629640` 修复，以下为 7b34f42 旧状】
SubAgentRunner.run_reserved_agent 调 host.task_manager.create_subagent(...) 与 update_subagent(...)：


spawn.py
Lines 1355-1358
        rec = host.task_manager.create_subagent(
            type=agent_type, description=agent_type, model=model or host.model,
            provider=host._current_provider())
        host.task_manager.update_subagent(rec.id, status="running")
但 TaskManager 根本没有这两个方法（src/nanocode/tasks/manager.py 仅有 create_task/update_task/get_task/list_tasks/...）。唯一调用方 memory_evolution/agents.py 把异常吞掉：


agents.py
Lines 43-46
            fut.cancel()
            return []
        except Exception:
            return []  # diagnosis is best-effort; failure never blocks optimization
用户后果：memory-evolution 的 retrieval 诊断子 agent 永远静默返回 []，诊断功能事实上从不工作。
架构后果：存在一条 typed runtime API（run_reserved_subagent）指向不存在的实现，是教科书级 contract drift。
违反理念：违反“发现契约漂移就修 producer/test seam，不留 permissive fallback”——这里连 fallback 都没有，是裸 AttributeError 被吞。
修复难度：低（要么补 TaskManager.create_subagent，要么把 reserved 路径并入 run_record 体系）。
D2. 双账本：run_record（child session）与 TaskManager（host task）并存（Verified，中）　→【v2(§0.1)：✅ 已由 Phase A 修复（单账本），以下为旧状】
agent 工具的 subagent → child session + subagent-run/（run_record.py）。
memory curator / eval → 同时建 run_record（begin_run_record）和 host task（task_manager.create_task，spawn.py:1056、1201）。
shell 后台 / memory_optimize 扩展任务 → 仅 host task。
running_background_count() 必须同时数两套（subagent_manager.py:25-49）。
后果：并发上限、状态展示、取消语义分裂在两个数据模型上；/agents 看 run_record，task_list/task_output 看 TaskManager，二者互不可见。用户必须知道“这是 agent 还是 task”才能找对查询命令。
违反理念：理念 #7“不要把 storage authority 从 session tree 转移到 task artifact”——curator 这类 subagent 的权威被劈成 run_record + TaskRecord 两份。
D3. 后台子 agent 无法把“需要确认”升级回父（Verified，高，体验硬伤）

spawn.py
Lines 60-62
async def _auto_deny_confirm(_command: str) -> bool:
    """后台子 agent 的 confirm_fn：无 TTY 等价拒绝（auto-deny-but-continue）。"""
    return False
后台 subagent 的危险操作一律 auto-deny 并继续，没有“blocked-needs-approval”状态，父无从介入。

对标：Claude Code 在 v2.1.186 之前正是这个行为，之后改为“后台子 agent 把确认 prompt 升级到主会话，标明哪个子 agent 在问，Esc 拒单次而不杀子”（docs-derived）。nanocode 现在 = Claude Code 的旧版本。pi-subagents 用 pi-intercom 的 contact_supervisor(reason:"need_decision") 解决同一问题。
后果：后台跑 coder 想 git push / 删文件 → 静默失败，用户事后才发现“它没做”。
D4. 后台 run 不跨进程存活，只能终态 resume（Verified，高）
run_cancel 只能取消“当前进程里有活 coroutine”的 run，否则 mark_lost（engine.py:740-756）；rebind 时 reconcile_for_parent 把所有非终态且无 live coroutine 的 child 标 lost（runs/ledger.py:40-56）。agent 工具的 resume 只接受非 running 的终态 run（spawn.py:618-620）。

后果：进程重启 / /resume 切会话后，在飞的后台 subagent 全部丢失（lost），无法续跑，只能对“已终态”的 run 追加新 prompt。
对标：Codex 的 LiveThread+ThreadStore（commit #18882）让 thread 持久化、可 thread/resume 重连在飞会话；pi-subagents 的 resume 通过 status.json+持久 .jsonl 复活子（虽也是“起新进程从旧 session 继续”，但至少能复活）；@quintinshaw 的 journaled resume 直接“重放已完成 agent、只跑没跑完的”，survives restart。nanocode 这块最弱。
D5. steer 的 wake 是半截契约（Verified，中）　→【v2(§0.1)：✅ 已由 Phase A 修复（wake 全删），以下为旧状】
agent 工具 schema 宣传 wake：“whether to wake an idle child”（agent.py:35）。但 AgentRunRuntime.send：


runtime.py
Lines 54-55
        if wake and rec.status != "running":
            raise RuntimeError(f"run {child_session_id} is not live-running; use agent resume to wake it")
wake=True 对非 running 的子直接抛错——根本叫不醒空闲/终态子。真正的“唤醒”只能走 resume。steer 的 live 注入也只发生在子自己仍在 loop时（session/agent.py:148-159 的 drain_pending_steers）。

后果：模型按 schema 以为能 wake 一个 idle 子，实际报错；契约与实现不符。
D6. chain / parallel 占满父 turn，非后台、无增量可见（Verified，中）
execute_agent_chain（顺序 await）与 execute_agent_parallel（asyncio.gather）都在父的同一个 tool 调用里同步跑完（spawn.py:802-877），父 turn 被整体阻塞，结果是一坨拼接字符串：


spawn.py
Lines 837-838
            previous = envelope
            sections.append(f"## Step {i}/{n} [{agent_type}] {description}\n{envelope}")
后果：跑 8 个并行 coder = 父会话卡死直到全部结束，期间只能靠 /agents widget 看；没有 pi-subagents 的“parallel 背景化 + 分组进度卡 + failFast/concurrency 细控 + 动态 fanout(expand/collect)”。
注：每步确实是独立 child session + run record（这点好），但编排本身不可后台、不可对单步 steer。
D7. TeamRuntime 是未接线的死骨架（Verified，中）
runtime/teams.py 全量为内存实现（ClaimLock/TeamTaskBoard/AgentMailbox/SharedArtifactStore），不持久、未注册任何工具、无任何调用方。session/tree.py 预留了 TEAM_* entry 类型但同样未落地。

后果：planner-worker / reviewer-worker / swarm / 对等 mailbox 协作在 nanocode = 完全没有（只有“父委派子、子汇报父”的星型）。pi-crew / @e9n pool+orchestrator / pi-subagents+intercom 都已落地对等或池化协作。
违反理念：不算违反，但属于“理念上预留、功能上空白”，对外是能力缺口。
D8. 全盘 O(N) header 扫描在热路径反复触发（Verified，中，性能）　→【v2(§0.1)：① 重绘写 lost 已修；② 派生索引 cache 仍未做且待计测】
children()→_scan_headers() 遍历 sessions_dir() 下所有 session 目录读首行（manager.py:349-376）。而 subagent_widget_snapshot()→_subagent_records()→_run_runtime.list()→reconcile_for_parent→children()（facade.py:805-809 / runs/runtime.py:33 / ledger.py:48），且 TUI footer + /agents 页高频重绘调用它，每次都 O(总 session 数) 全盘扫描 + 逐 child 打开 session + 读 sidecar。

后果：session 库一大，TUI 卡顿；reconcile 还会在扫描中对“看起来没 live coroutine 的非终态 run”写 lost（潜在误判 + 写放大）。
D9. 子 trajectory_id 不分叉，child trace 归属父（Verified，低-中）

spawn.py
Lines 9-10
- `session_id == _tree_session_id` 共享 trajectory_id（独立 child sid 由 _tree_session_id 承载,
  session_id 仍 parent-keyed,故 trajectory_id 不分叉）;
build_sub_agent 里 session_id=session_id or host.session_id（spawn.py:212），child 的 session_id 仍是父键，trajectory 不分叉。

后果：trace/eval 产物层面，child 的 step 归到父 trajectory_id，无法干净地按 child 单独导出/评估一条子轨迹。理念 #4 要求“child 显式继承 trajectory”，但“继承”被实现成“共享同一 id”，与“可独立评估每条子轨迹”冲突。这是一个需要明确取舍的点。
D10. 父信封确实有界，但 /agents 详情页直接重读 child 的 session.jsonl（Verified，中等，边界争议）
父 branch 只存 bounded ResultEnvelope（≤4KB，agents/result.py + agent_result.render_agent_result_envelope）——这点符合理念 #3。但 /agents 的 transcript viewer：


facade.py
Lines 783-789
        mgr = SessionManager.open(child_session_id)
        messages = [
            dict(e.data.get("message") or {})
            for e in mgr.entries()
            if e.type == T.MESSAGE
        ]
        return {"record": record.to_dict(), "messages": messages}
这是经 facade（满足理念 #5 的“TUI 不直接读底层文件”——TUI 调 thread.subagent_conversation_snapshot），但 facade 自己 SessionManager.open(child) 全量读 child transcript。严格说没违反“TUI 绕过 facade”，但它把“父不默认看完整 child transcript”的边界放在了 UI 自愿层而非数据层——任何 facade 调用方都能拿到完整 child transcript。这是可接受的设计，但要意识到 envelope 边界只是“父上下文”边界，不是“可见性”边界。

D11. run_record 重算一套 metrics，存在与 session truth 漂移的窗口（Verified，低）
attach_run_record_projector（spawn.py:252-307）把 child UI 事件投影进 sidecar，run_record.py 自己维护 toolUses/turnCount/usage/activeTools。projector 是 best-effort（订阅者异常被 _push 吞，facade.py:417-421）。

后果：sidecar metrics 与 canonical session 可能漂移（虽 session 仍是 truth，但 /agents 显示的是 sidecar）。可接受，但属于“第二份派生状态”的维护成本。
4. 同行实现对比表
缩写：CS=child session 是否 first-class first-class；PM=parent metadata；BG=background；Orch=chain/parallel/planner/reviewer/swarm；Ctrl=cancel/resume/steer；Sbx=sandbox/approval 继承；Iso=tool allowlist/隔离；Env=parent-visible envelope；Vis=child transcript 可见性；List=child 是否污染顶层列表；WT=worktree；Recov=失败恢复/长活。

系统	CS	PM	BG	Orch	Ctrl	Sbx/approval	Iso	Env	Vis	List	WT	Recov
nanocode
✅ 真 child session.jsonl(spawn.py:226-238)
header parentSession{sessionId,entryId,taskId,agentId}
✅ 单 run；chain/parallel 不后台
chain/parallel/{previous}/worktree；planner/reviewer=仅 prompt 约定；swarm=无(teams 死骨架)
cancel(live-only)/resume(终态-only)/steer(live-only)
子继承父 mode+sandbox_profile；BG auto-deny
typed allow∩/deny∪/剔 agent(permissions.py)
✅ ≤4KB ResultEnvelope
facade 可全读 child tree
✅ 排除 agentId 子(listing.py:81-83)
✅(worktree.py)
❌ 重启即 lost；无 journal
OpenCode
✅ Session.create({parentID})
parentID(+session permission override)
✅(实验 flag) task_id 返回即走
task 单委派；background；无原生 chain/parallel DSL
cancel(级联子 by metadata.sessionId)/resume(by task_id)/steer=继续同 session
session 级 permission override(deny task/todo)
session permission allow/deny
✅ <task_result>+task_id
子 session 可导航
子 session 嵌套/隐藏
插件级
SQLite 持久 thread
Pi core
✅ SessionManager tree + AgentSessionRuntime
header parentSession；SessionManager tree API
❌ core 无
❌ core 无 subagent（设计如此）
new/resume/fork/switch on runtime
无 OS sandbox（全系统访问）
tool registry/active tools
n/a(core)
n/a
n/a
❌ core 无
rollout/session 文件
Pi official examples
n/a
n/a
n/a
n/a
13-session-runtime.ts 演示 switch
n/a
n/a
n/a
n/a
n/a
n/a
n/a
pi-subagents (nicobailon)
✅ in-proc child Pi session；fork=真分支(--session from leaf)
session 派生；nested 在父 status 树
✅ status.json/events.jsonl/output.log + async-complete 事件
single/parallel(count,concurrency,failFast)/chain(+静态 parallel 组 + 动态 expand/collect)/saved .chain.md/.json
status/interrupt/resume(by id/index/nested)；resume=从 session 文件复活
deny-at-use；BG 可经 intercom 问父
tools allowlist；child 默认无 subagent 工具；maxSubagentDepth
maxOutput 截断 + outputMode:file-only(指针) + structured schema
child 不收 bundled skill；fork context 过滤父 artifact
独立 session dir
✅ worktree:true + setupHook + diff/patch artifact
部分（resume 复活，非真续进程）
@tintinweb/pi-subagents
✅ in-proc
session
✅
Claude-Code 式自治；batteries(scheduling/RPC/model-scope)
resume/steer
内置 disallowed_tools + model-scope
内置 denylist
bounded
—
—
—
—
@gotgenes/pi-subagents
✅ in-proc(无子进程)
session-created 事件(pre-bind)
✅ foreground/background
minimal core；orch 由调用方组合
steer/resume completed
deny-at-use；权限委托 @gotgenes/pi-permission-system(订阅 lifecycle 事件)
typed SubagentsService；worktree 委托 pi-subagents-worktrees(WorkspaceProvider)
typed result
lifecycle events(created/started/completed/failed/steered/compacted)
—
委托 companion 包
—
@quintinshaw/pi-dynamic-workflows
✅ in-mem Pi session(变量持有)
n/a(脚本变量)
✅ 默认非阻塞 + live panel
code-mode：vm 沙箱跑 JS agent()/parallel()/pipeline()/phase()/verify()/judgePanel()/loopUntilDry()/checkpoint()；≤16 并发/1000 总
checkpoint(journaled 人工门)/abort
tier/model 路由
schema(bounded repair)
结果只回综合值，中间留变量
/workflows 看 compact 历史
n/a
✅ src/worktree.ts .pi/worktrees/ 确定性名(runId+call idx)
✅✅ journaled resume(重放已完成 agent，survives restart)
michaelliv/pi-dynamic-workflows
✅ in-mem
n/a
✅
code-mode 原型(agent/parallel/pipeline/phase)
abort
budget
schema
变量
—
n/a
❌(原型未做)
❌(原型未做)
pi-sub-agent
⚠️ 子进程 pi --mode json -p --no-session
进程边界
⚠️
single/parallel/chain；agentScope user/project/both
—
confirmProjectAgents 门(非交互默认 block project agent)
--no-session/extension 隔离
stdout 收集
进程隔离
n/a
per-task cwd
进程级
@e9n/pi-subagent (espennilsen)
⚠️ 子进程
进程树
⚠️ pool 长活
single/parallel/chain/orchestrator(树,自治 spawn+message)/pool(spawn/send/list/kill)
pool send/kill/kill-all
--no-extensions 默认
extension allowlist
stream
进程隔离
n/a
—
pool 持久上下文
pi-intercom
n/a(通道)
—
—
—
contact_supervisor 子→父 need_decision/progress_update
—
—
grouped 回传
—
—
—
—
Codex
✅ Thread(durable)；subagent thread
parent_thread_id ⟂ forked_from_id(PR#25113，正交)
✅ thread 持久(rollout.jsonl)；app-server 多 core session
并行 subagent；spawn_agents_on_csv(每行一 worker，batch→CSV)
/agent 切换/steer/stop/close；thread/rollback；turn 可被 approval 暂停
OS sandbox(Seatbelt/Landlock+seccomp)+approval_policy(untrusted/on-request/never)+named profile
agent TOML(~/.codex/agents)；max_threads/max_depth(默认1)/job_max_runtime_seconds
subagent 回 summary 非 raw
/agent 可 inspect
thread/list filter parentThreadId
(环境/worktree 经 environments)
✅ ThreadStore/LiveThread 持久+resume+fork+archive
Claude Code
✅ 子 agent 独立 context+transcript(独立文件)
parent_tool_use_id；resume sessionId 访问子 transcript
✅ background subagent
Agent 工具委派；并行；fork 继承父
maxTurns；resume session
子继承父权限；BG 子(v2.1.186+)把确认升级回主会话(Esc 拒单次不杀子)
tools/disallowedTools/permissionMode/mcpServers
单条 text result 回父，父不见中间 tool 调用
默认只回最终消息；resume 同 session 看 transcript
子 transcript 独立文件，不污染主
✅ isolation:worktree
✅ 子 transcript 持久+resume(cleanupPeriodDays=30)
5. 可借鉴能力清单
#	来源	源码证据	能力	nanocode 现状	差距	建议借鉴方式	风险
C1
Claude Code
docs：BG 子 v2.1.186 升级确认回主会话
后台子 agent 把“需确认”升级回父，Esc 拒单次不杀子
仅 auto-deny(spawn.py:60)
缺“blocked-needs-approval”态 + 升级通道
run_record 增 blocked 状态 + pending_approval 队列；父在 /agents 应答（见方案 A）
父需在线；要防确认风暴
C2
Codex
PR#25113 parent_thread_id ⟂ forked_from_id
subagent 血缘与 fork 血缘正交两字段
nanocode parentSession 把 {taskId,agentId,forkedBeforeEntryId} 混在一个 dict
语义混用：fork 与 subagent 共用 parentSession
header 拆 subagentParent vs forkLineage；listing 仍按前者隐藏
迁移旧 header（需 producer 修正，不留 alias）
C3
pi-subagents
README：acceptance gates（verified=runtime 跑命令，非子自报）
验收门：none/attested/checked/verified/reviewed，runtime 跑校验命令
无（父只拿 envelope，无客观验收）
child“说做完了”≠真做完
envelope 增 acceptance{level,evidence,verify[]}；host 跑命令判定
增执行成本；命令需 sandbox
C4
pi-subagents+intercom
README：contact_supervisor(reason)
子→父私有协调通道（need_decision/progress_update），不进父 transcript
只有 steer(父→子)，无子→父
单向
复用 mailbox(teams.py 已有内存原型) + run_record pending_question
必须 bounded、不 overload 父 transcript
C5
@quintinshaw
commit b781fca worktree.ts + journaled resume
journaled resume：重放已完成 agent，survives restart
重启即 lost(ledger.py:53)
后台/编排不可续
run_record 已有 events.jsonl → 升级为 replay journal；编排步骤幂等键
journal 与 session truth 一致性
C6
Codex
app-server README + #18882 LiveThread
持久 thread + 在飞 resume/fork/archive 经 ThreadStore
child session 有 .jsonl 但无在飞重连
进程重启丢在飞
LiveThread 式“session-owned persistence handle”，rebind 时重连 running run
复杂；与单写者锁交互
C7
pi-subagents
README：outputMode file-only + structured outputSchema
结果指针模式 + 结构化输出校验
envelope 截断+result_path 指针(已有)，但无 schema 校验
子输出无 typed 契约
run_fresh_subagent 增 output_schema，校验后回结构对象
子需配合；校验失败处理
C8
pi-subagents / @quintinshaw
README：动态 expand/collect、pipeline
动态 fan-out（从结构化输出展开 N 子）+ pipeline
仅静态 steps/tasks(spawn.py:787-788 cap)
不能按上一步结果动态分裂
chain 增 expand{from,maxItems}/collect{as}
防 fan-out 爆炸（maxItems 必填）
C9
@gotgenes
ADR 0002：minimal core + lifecycle events，权限/worktree 委托 companion
core 发 lifecycle 事件，reactive 关注点订阅（permission/telemetry/UI 不改 core）
事件总线已有(facade _event_subscribers)，但 subagent lifecycle 未结构化暴露给扩展
扩展拿不到 subagent 生命周期
把 SubAgentStarted/Ended + created/completed 暴露为稳定 extension 事件
事件契约一旦公开难改
C10
Codex
docs：spawn_agents_on_csv 每行一 worker→CSV
数据驱动批量 fan-out + 结构化导出
无
批处理场景缺失
作为 first-party extension（方案 C/E）
资源/成本控制
C11
Pi core
extensions.md：pi.registerTool/pi.on/ctx.ui；sdk.md：AgentSessionRuntime
稳定 extension runtime API（事件+工具+UI+session 树只读）
nanocode 有 ExtensionHost(facade.py:236-283) 但 subagent/编排能力未作为 extension API 暴露
扩展无法构建 multi-agent
暴露 runtime.spawn_subagent / subscribe / readonly_session（方案 C）
扩展安全边界(UNTRUSTED)
C12
OpenCode
task.ts output(sessionID,text)；task_status.ts
极简 envelope（task_id + <task_result>）+ 独立 task_status 工具　【v2(§0.2) 更正：`task_status.ts` 不存在且被显式不注册；状态走 SSE 事件】
nanocode envelope 更重但等价；get_subagent_result=run_output 别名
基本对齐（nanocode 这块不弱）
维持现状即可
—
6. 不应照搬的能力
来源能力	为何诱人	为何不适合 nanocode	若要做如何改造
pi-sub-agent / @e9n 子进程 pi --mode json 委派
强隔离、崩溃不连坐
nanocode 是嵌入式单进程、canonical session.jsonl 单写者锁；起子进程会出现两个写者抢 child session 锁，破坏 SessionLease 模型；且 Python 冷启动昂贵
若要进程隔离，只用于 worktree+shell，子 agent 仍 in-proc（保持单写者）；进程化仅作 external adapter（方案 E）
@quintinshaw code-mode（模型写 JS 在 vm 跑编排）
极强表达力、Claude-Code 同款
nanocode 无 JS vm；引入 = 在 Python 里塞 JS 沙箱，巨大攻击面，且与“canonical replayable session”冲突（脚本变量态不在 session tree 里，不可 replay）
若要，改成 声明式 DAG（JSON workflow），编排器是 Python、每节点是审计过的 host 原语，态落 session/run journal（方案 D）
Pi core “core 不内置 subagent，全靠 extension + 全系统访问”
极简内核、生态繁荣
nanocode 的卖点正是带 OS sandbox + 权限派生 + session-tree authority 的内核；学 Pi 把安全边界外包给扩展会丢掉差异化，且 Pi 自己警告“包有全系统访问，装前读源码”
借API 形态（registerTool/事件/只读 session），但安全 primitive 留 core（方案 C）
Claude Code fork 子默认继承父全对话
上下文连续
与“parent 只看 bounded envelope、child 隔离上下文”理念冲突；fork 继承会让 child 上下文爆炸、且把父 transcript 复制进 child
维持 nanocode context.mode={fresh,fork_summary,branch_projection}（spawn.py:82-113）——摘要/投影而非全量 fork，是更克制的正确做法
OpenCode SQLite 作 session store
查询快、列表廉价
违反理念 #1/#7（session.jsonl 是 truth）；引入 SQLite = 第二事实源
只把 SQLite/索引作派生 cache（可重建），解决 D8 的 O(N) 扫描，但绝不作 authority（方案 A 的索引子项）
@e9n orchestrator 树（子自治 spawn 孙、对等 message）
真 swarm
nanocode 结构性禁止孙（剔 agent 工具，spawn.py:159/199），且 TeamRuntime 未落地；贸然放开深度=成本与可预测性黑洞（Codex 默认 max_depth=1 也是这个理由）
若做对等协作，走 TeamRuntime（方案 D），depth 默认 1、显式 opt-in，态落 team session entry
7. 多个改造方案
方案 A：最小修复型（修契约债 + 补 subagent 体验）
目标：不动 session 模型，修掉 §3 的硬债 + 把 status/result/cancel/resume/steer 的契约补全。

内容
修 D1：删除/重写 run_reserved_agent，把 reserved-agent（memory 诊断/优化）并入 run_record 体系（与 curator 一致），删 task_manager.create_subagent 幽灵调用；补测试覆盖该路径。
修 D2 半步：让 /agents 与 task_* 互相可见——agents_overview 同时列 host task 与 run_record，或把 curator 的 host-task 镜像隐藏、只留 run_record（统一为 child session 账本）。
修 D5：wake 契约对齐——要么实现“对 idle 子重新调度 loop”，要么从 schema 删 wake、明确 steer 只对 live 子，resume 才唤醒。不留误导 schema。
修 D8：给 children() 加派生索引 cache（parent_sid → [child_sid]，可重建、非 authority），TUI 重绘走 cache，避免每帧全盘扫描。
补 D11/可观测：run_record metrics 与 session truth 加一致性自检测试（projector 异常时标记 sidecar stale，UI 显示提示而非静默漂移）。
优点：低风险、纯修复，把“看起来能用其实断裂”的路径变成真能用；不碰理念。
缺点：不解决后台长活、子→父确认、swarm；体验仍落后 Claude Code/pi-subagents。
适合：想先止血、保持嵌入式纯净。不适合：想要 multi-agent 竞争力。
涉及文件：runtime/spawn.py、tasks/manager.py、tools/agent.py、runs/runtime.py、session/manager.py(索引)、tui/session_pages/agents.py、extensions/memory_evolution/agents.py。
测试计划：reserved-agent 端到端（不再 AttributeError）；wake 契约测试；children() 索引一致性（cache vs 全扫描等价）；curator 双账本可见性。
风险/回滚：索引 cache 加“校验模式”双跑比对，发现不一致即回退到全扫描；其余为局部改动，按文件回滚。
方案 B：OpenCode 式后台 task envelope + child 导航（保 session-tree authority）
目标：借 OpenCode 的 task_id 即 child session、<task_result> envelope、background BackgroundJob、cancel 级联、/agent 导航；但 storage 仍是 session.jsonl 树。

id 关系（关键）：沿用 nanocode 现有统一——task_id == child_session_id == run_id（runs/models.py 已如此）。新增把 chain/parallel 也后台化：每个编排为一个 orchestration_run（自身一个 child-less 协调 run_record），其下每步/每任务是独立 child run（parentRun=orchestration_id）。run_id 仍是 child session id，orchestration_id 是协调器 id（不持 session，只持 run_record，明确标 kind=orchestration）。
内容：chain/parallel 支持 run_in_background（解 D6）；/agents 增“跳转到 child session（resume 视图）”；cancel 级联（取消 orchestration → 取消其下所有 live child，对标 OpenCode run-state cancel by metadata）。
优点：后台编排 + 导航大幅改善体验；不引第二事实源。
缺点：引入 orchestration_run 这层协调记录（需明确它不是 session、只是 run sidecar）；并发与取消语义变复杂。
风险：orchestration_run 若被误当 authority 会侵蚀理念 #7——必须在 models 注释/测试里钉死“它是派生协调态，可重建”。
涉及文件：runtime/spawn.py(chain/parallel 后台化)、runs/models.py(orchestration kind)、runs/ledger.py/runtime.py(级联 cancel)、tui/session_pages/agents.py、runtime/facade.py(导航 API)。
测试：后台 chain 完成回注；cancel 级联；orchestration_run 重建幂等；child 仍不污染顶层 listing。
方案 C：Pi-extension 式编排基底（core primitives + first-party extension）
目标：把“multi-agent/workflow”能力主要做成 first-party extension，core 只暴露稳定 runtime API；对标 @gotgenes 的“minimal core + lifecycle events + companion 包”。

core 留什么（primitives）：spawn_subagent(单 child)、bounded envelope、权限派生（permissions.py）、sandbox、SessionLease、run_record、lifecycle 事件（created/started/completed/failed/steered/compacted，对标 @gotgenes ADR 0002 / Pi pi.events）。
extension 做什么（capabilities）：chain/parallel/动态 fanout/planner-worker/reviewer-loop/acceptance/CSV-batch —— 全部经 runtime.spawn_subagent + subscribe 组合。
需 core 暴露的 API：RuntimeThread.spawn_subagent(profile,prompt,ctx_mode,isolation,background) -> run_id、subscribe(subagent_lifecycle)、readonly_session(child)、runs.status/output/cancel/send（多数已在 facade 存在，需稳定化为公开 extension 面）。
优点：core 不变重、能力解耦、社区可扩展；最契合 nanocode 已有 ExtensionHost。
缺点：对嵌入式边界有压力——extension 默认 UNTRUSTED（facade.py:268），要让它能 spawn 子又不破坏权限派生，需要给 extension 一个受信的 spawn 能力槽（明确 opt-in、审计）。
对嵌入式边界影响：必须保证 extension spawn 的子仍走 core 的 effective_child_tools + sandbox 派生，不能让 extension 绕过；这是成败关键。
风险：公开 API 一旦稳定难改；extension 安全。
涉及文件：runtime/facade.py(公开 spawn/subscribe)、extensions/*(新 first-party orchestration extension)、agents/permissions.py(extension 子派生)、runtime/spawn.py(被复用)。
测试：extension spawn 的子工具集 = core 派生集（不可提权）；lifecycle 事件契约；extension 失效不影响 core。
方案 D：完整 multi-agent orchestration engine（planner-worker/parallel/chain/reviewer/worktree/long-lived）
目标：落地真正的多 agent，含对等协作（激活 TeamRuntime）、声明式 DAG workflow、journaled resume、reviewer/planner 角色、worktree 隔离、长活协调态。

内容：声明式 workflow（JSON DAG，节点=审计过的 host 原语，对标 pi-subagents .chain.json expand/collect 而非 @quintinshaw 的 JS-vm）；TeamRuntime 落盘（team session + TEAM_* entry，board/claim/mailbox 持久化，解 D7）；journaled resume（events.jsonl 升级为 replay journal，对标 @quintinshaw，解 D4）；reviewer-loop / acceptance gates（C3）；intercom 式子→父（C4）。
是否过重：是，明显过重。这是“把 nanocode 从 coding agent 变成 orchestration platform”。除非有强需求，不建议一次到位。
优点：功能面追平甚至超过 pi-subagents+pi-crew。
缺点/复杂度：DAG 引擎 + team 持久化 + journal replay + 对等 mailbox = 大量新状态机；测试矩阵爆炸。
失败模式：fan-out 爆炸（必须 maxItems/concurrency/depth 三重 cap）；journal 与 session truth 漂移；team mailbox overload 父 transcript；worktree 泄漏分支。
涉及文件：runtime/teams.py(落盘+接线)、session/tree.py(TEAM_* 落地)、新 runtime/workflow.py(DAG)、subagents/run_record.py(journal)、runtime/spawn.py、tui/session_pages/agents.py(team 视图)。
测试：DAG 幂等 resume；claim 无双认领（teams.py 已有 ClaimLock 测试基础）；mailbox 不进父 transcript；worktree 全清理。
方案 E（反向选择）：不增强 core，只做外部 adapter / extension
目标：core 保持现状（甚至砍 TeamRuntime 死骨架），multi-agent 全部交给外部进程编排（如 nanocode CLI 的 --print/json 模式被外部 orchestrator 调度，类比 pi-sub-agent 子进程模型）。

为何更务实：nanocode 已有 entrypoints（含 print/JSON 设想）；外部编排不污染 core、不碰单写者锁（每个外部进程开独立顶层 session，非 child）。
体验损失：失去“父会话内 /agents 统一视图、bounded envelope 回注、子→父 steer”——变成纯进程编排，用户体验割裂；child 不再是父 session 的 first-class child（退化为平级 session）。
如何避免 core 变复杂：core 只需稳定 --print --json 协议 + session resume；编排器是独立工具/extension。
适合：明确不想把 orchestration 责任放进嵌入式 core 的团队。代价：与理念 #2（subagent 应是 first-class child）正面冲突——等于承认“nanocode 不做 multi-agent”。
8. 方案横向对比
维度	A 最小修复	B OpenCode 式后台+导航	C Pi-extension 基底	D 完整 engine	E 外部 adapter
解决的核心问题
契约债/断裂路径/扫描性能
后台编排 + child 导航
能力解耦 + 可扩展
全功能 multi-agent
core 零负担
未解决
后台长活/子→父/ swarm
swarm/journal/acceptance
长活 resume(除非加)
（几乎全覆盖）
几乎全部体验
对理念冲击
无（强化理念）
低（需钉死 orchestration_run 非 authority）
中（extension 受信 spawn 槽）
高（team/journal 新状态源，需严守 session authority）
高（弃理念 #2）
用户体验提升
小
大
中（取决扩展）
最大
负（割裂）
实现复杂度
低
中
中-高
很高
低（core）/外移
测试复杂度
低
中
中
很高
低
嵌入式边界风险
低
低
中-高（extension spawn 不可提权）
中
低（但靠多进程）
长期扩展性
低
中
高
高
中（外部生态）
推荐程度
必做（地基）
强推
推（中期）
谨慎/按需
仅特定团队
客观提醒：B+C 合起来才达到 pi-subagents 今天的功能水位；单独任一方案都还落后 pi-subagents（其 acceptance gates / intercom / 动态 fanout / nested 深度 / file-only 输出是 nanocode 全无的），更落后 Codex 的持久 thread + OS sandbox 组合。不能用“session-tree 更纯”掩盖这些功能缺口。

9. 推荐路线
主推荐：A（第一阶段，必做）→ B + C1(确认升级) + C2(血缘正交)（第二阶段），即“先修债，再补后台编排 + 后台子 agent 确认升级 + parent/fork 血缘拆分”。
备选推荐：A → C（extension 基底），把后续 multi-agent 都做成 first-party extension，core 只稳定 API。
理由：

为什么不是最保守（纯 A）：A 不修 D3（子→父确认）和 D6（后台编排），而这俩正是 nanocode 相对 Claude Code/pi-subagents 最刺眼的体验差距，光止血不够。
为什么不是最大（D/E）：D 过重且高风险，E 直接放弃理念 #2。现阶段不值得。
第一阶段必修：D1（断裂路径）、D2（双账本可见性）、D5（wake 契约）、D8（扫描性能）——这些是“正确性/契约”问题，不修会持续误导模型与用户。
可延后：D9（trajectory 分叉）、acceptance gates（C3）、动态 fanout（C8）、journaled resume（C5）。
不建议做：code-mode JS vm、子进程委派、对等 swarm 全量落地（除非有明确需求）。
要快速见效：选 B（后台 chain/parallel + /agents 跳转 + cancel 级联），用户立刻感知。
要长期架构：选 C（extension 基底 + lifecycle 事件），对标 @gotgenes 的“minimal core + companion”分层。
10. 目标架构图（主推荐 A→B+C1+C2 后形态）
Parent Session (session.jsonl = TRUTH)
Core Runtime primitives
Child Session (first-class, 独立 session.jsonl)
Orchestration run (派生, 非 authority)
确认升级 (C1)
TUI (只读 snapshot, 经 facade)
Extension layer (方案C, UNTRUSTED + 受信 spawn 槽)
Trajectory / Trace / Eval
危险确认
bounded
resume/steer/cancel
children(parent) via header scan + 派生索引 cache (D8)
RuntimeThread (facade)
agent / run_* tools
bounded ResultEnvelope <=4KB (父 branch 仅存信封)
SubAgentRunner.spawn (权限派生 effective_child_tools + sandbox)
SessionLease (单写者锁)
lifecycle events: created/started/completed/failed/steered/compacted
child session tree (canonical)
subagent-run/ sidecar (status.json/events.jsonl/result.md) = 派生投影
header: subagentParent ⟂ forkLineage (C2 拆分)
orchestration_run kind=chain/parallel
background detached + cancel 级联
run_record.status=blocked + pending_approval
升级回父 /agents (Esc 拒单次, 不杀子)
subagent widget
/agents: status/result/cancel/resume/steer/导航
first-party orchestration ext (planner/reviewer/acceptance/CSV)
per-child trajectory (D9 可选分叉)
11. 分阶段落地计划
阶段 0：契约止血（对应方案 A）
目标：消灭断裂路径与误导契约。
修改文件：runtime/spawn.py(run_reserved_agent)、tasks/manager.py、tools/agent.py(wake)、runs/runtime.py、session/manager.py(children 索引)、extensions/memory_evolution/agents.py。
数据结构变化：run_record 增 kind(subagent|orchestration|reserved)；新增派生索引（可重建）。
runtime contract 变化：reserved-agent 走 run_record；wake 语义对齐或移除。
UI/TUI 变化：agents_overview 合并 host task + run_record。
测试：reserved-agent 端到端；wake；索引 vs 全扫描等价。
验收：诊断子 agent 真正返回建议；无 AttributeError；TUI 列表覆盖两类。
回滚：索引双跑校验，异常回退全扫描。
不做：不碰 session 模型、不动 envelope。
阶段 1：后台编排 + child 导航（方案 B）
目标：chain/parallel 可后台 + /agents 跳转 + cancel 级联。
修改文件：runtime/spawn.py、runs/models.py/ledger.py/runtime.py、runtime/facade.py、tui/session_pages/agents.py。
数据结构：orchestration_run(kind=chain/parallel, children=[run_id])。
contract：agent schema 允许 steps/tasks + run_in_background；cancel 级联。
测试：后台 chain 回注；级联 cancel；orchestration_run 重建幂等；顶层 listing 仍排除子。
验收：8 并行 coder 不阻塞父 turn；/agents 可跳进 child 续跑。
回滚：feature flag 关闭后台编排，回退同步路径。
不做：不引 SQLite authority；不做 swarm。
阶段 2：后台子 agent 确认升级 + 血缘正交（C1+C2）
目标：后台子需确认时升级回父；拆 subagent/fork 血缘。
修改文件：runtime/spawn.py(confirm_fn 改为“升级而非 auto-deny”)、subagents/run_record.py(blocked/pending_approval)、session/manager.py(header 字段)、tui/session_pages/agents.py、session/listing.py。
数据结构：header subagentParent 与 forkLineage 分离；run_record blocked 态 + pending_approval 队列。
contract：后台 confirm → 写 blocked + 升级事件；父应答 allow/deny（单次）。
UI：/agents 显示待确认、支持 allow/deny。
测试：后台危险操作升级；Esc 拒单次不杀子；旧 header 迁移（producer 修正，不留 alias）；listing 仍按 subagentParent 隐藏。
验收：后台 coder 的 git push 能在父侧获批后真正执行。
回滚：confirm 升级 feature flag，回退 auto-deny。
不做：不做无人值守自动批准。
阶段 3（按需）：extension 基底 / acceptance / 动态 fanout / journal（C3/C5/C8/C9/C11）
仅在有需求时启动，按 C 或 D 局部推进；每项独立 flag。
12. 测试和验证计划
# 0. 干净度
git diff --check
# 1. focused（subagent / runtime / listing / steer）
.venv/bin/python -m pytest tests -k "subagent or spawn or run_record or runs or listing or steer or facade" -q
# 2. full
.venv/bin/python -m pytest -q
必须覆盖的验证项：

session artifact 验证：spawn 后 child session.jsonl 存在且 header 含 subagentParent；run_record subagent-run/{status.json,events.jsonl,result.md} 一致。
permission/sandbox 继承：effective_child_tools = 父 allow∩子 deny∪剔 agent（断言子绝不获得父没有的工具）；child sandbox_profile == 父。
background auto-deny / 升级：阶段 2 前——后台危险确认 auto-deny；阶段 2 后——升级为 blocked 且父可应答。
child 不污染顶层 listing：建 subagent 后 scan_sessions() 不含该 child（listing.py:81-83 行为回归测试）；fork/clone 子仍出现。
result retrieval / cancel / resume / steer：run_output/get_subagent_result 取 bounded envelope；run_cancel 对 live 取消、对无 coroutine mark_lost；resume 拒绝 running、接受终态；steer live 注入、wake 契约。
worktree isolation：parallel coder/general 自动 worktree（should_isolate）；finalize 写 diff_summary；分支不泄漏。
D1 回归：reserved-agent 路径不再抛 AttributeError，诊断返回非空。
D8 性能/一致性：children 索引 cache 与全扫描结果逐项相等。
手工 TUI 验证：/agents 列表→详情→resume/steer/cancel；footer widget 后台进度；后台编排不阻塞父 turn。
13. 结论分级
Verified from source（nanocode）
subagent 是真 first-class child session（独立 session.jsonl + parentSession header）；run_id=child_session_id=task_id 三者统一。
run_record 是 child session 目录下的 sidecar 投影，session.jsonl 仍是 truth。
权限/工具 typed 派生（child≤parent，allow∩/deny∪/剔 agent），sandbox_profile 继承。
child 不污染顶层 listing（排除 agentId 子）。
D1 reserved-agent 路径断裂（task_manager.create_subagent 不存在，异常被吞）。
D2 双账本、D5 wake 半契约、D6 chain/parallel 占满父 turn、D7 TeamRuntime 死骨架、D8 O(N) 扫描热路径、D9 trajectory 不分叉、D4 重启即 lost。
Verified from source（同行核心）
OpenCode：Session.create({parentID}) + <task_result>+task_id envelope + 实验性 background + cancel 级联（task.ts / #27084 / run-state.ts）。
Codex：parent_thread_id ⟂ forked_from_id（PR#25113）；LiveThread/ThreadStore 持久（#18882）；app-server 可暂停 turn 等 approval；OS sandbox + approval_policy；agents.max_depth 默认 1。
Pi core：pi.registerTool/on/appendEntry、ctx.ui、AgentSessionRuntime（new/resume/fork/switch）、core 无 subagent（by design）。
@gotgenes ADR 0002：minimal core + lifecycle 事件 + companion 包（权限/worktree 委托）。
@quintinshaw worktree.ts（commit b781fca）+ journaled resume。
Docs-derived
Claude Code：Agent 工具单 text result/父不见中间；BG 子 v2.1.186 起确认升级回主会话（之前 auto-deny=nanocode 现状）；fork 继承父；isolation:worktree；hooks(SubagentStop/PreCompact/...)；子 transcript 独立文件+resume。
pi-subagents acceptance gates / intercom / 动态 fanout / file-only output / nested 深度 / fallbackModels（README 级，未逐 .ts 行）。
pi-sub-agent / @e9n 子进程 + pool/orchestrator；pi-crew 团队协作。
Inferred
OpenCode TUI 隐藏 child session 列表的精确实现（基于 parentID + 文档，未逐行）。
pi-subagents/@quintinshaw 实现细节（基于 README/commit，未逐 .ts 行读）。
Recommendation
主推 A→B+C1+C2；备选 A→C；不做 code-mode/子进程/全量 swarm。
Unknown / needs follow-up
nanocode 是否已有针对 D1 reserved-agent 的测试（搜索未见；需确认是否真无覆盖）。
@tintinweb/pi-subagents 的 scheduling/model-scope 具体实现（仅 docs 对比，未读源码）。
Claude Code 后台子 agent 升级确认的内部数据模型（闭源，仅行为文档）。
