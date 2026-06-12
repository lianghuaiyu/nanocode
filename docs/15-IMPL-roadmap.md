# docs/15 改造 — 落地路线图 (IMPL roadmap)

本文件是 docs/15 重构的**执行路线图**,由 8 子系统并行勘察 + 综合 agent 产出(2026-06-11)。
驱动全程自主落地。每完成一步勾选并更新状态。

## 基线
- 绿基线: **1240 passed, 4 skipped** (`.venv/bin/python -m pytest -q`, ~21s)。

## 进度记录（2026-06-11,全程自主推进中）
当前: **1325 passed, 4 skipped, 0 回归**。已提交 12 个 checkpoint（main 分支,显式 staging,无 Co-Authored-By）:
- `6305103` Phase 0: schemas + 29 契约测试（state/events/packs/ledger/cache_policy/profile/symbols）。
- `de916ad` Phase 1 STEP B: ProviderAdapter seam（providers.py;两 mixin 委托 self._provider.stream）。
- `71efd40` Phase 1 STEP C: AgentCore 抽出（core.py + loop.py;删 backend mixin;Agent MRO 收窄）。
- `58018de` Phase 2 STEP D: AgentSession state↔tree 同步（hydrate_state/record_event/verify_turn_consistency
  + assistant/toolResult 树写 required=True §7.6）。
- `f4cc23d` Phase 3 STEP E: ContextRuntime abstraction（providers/runtime/ledger/budgets）。
- `9641987` Phase 4 §9.3: read_file budget caps（offset/limit/byte+line cap/截断标记）。
- `c83d387` Phase 5: AgentProfile registry + child≤parent 权限派生（agents/permissions+registry）。
- `e02bc16` Phase 8: TeamRuntime 骨架 + 预留 team session entry 类型。
- `79cd35d` Phase 5: capabilities 层（PermissionContext 不可变 + dispatch taxonomy + 单一 allowlist 咽喉点）。
- `966840f` Phase 6 foundation: typed ResultEnvelope（host-derived files + 有界 render）。
- `31da199` Phase 3: /context 命令（packs + budget + survival matrix）。

codex 交叉验证: 对 Phase 1（loop 上移）跑了 codex xhigh review,**已确认 cancel/abort poll inventory
one-to-one 保留**（最高风险 #1 验证通过）;随后 codex runtime 在更广分析上挂起(~25min 无进展),已取消。
Phase 1 另有自身 characterization 测试(test_providers.py + 全套 e2e via fake provider)作回归网。

### 新架构落地状态（docs/15 §5 目标模块）
**已建 + 测试**: agent/{state,events,providers,loop,core}.py · session/agent_session.py(promoted) ·
context/{packs,ledger,cache_policy,budgets,providers,runtime}.py · agents/{profile,registry,permissions,result}.py ·
capabilities/{permissions,router}.py · codeintel/symbols.py · runtime/teams.py · tools/read_file.py(caps)。

### 各 Phase 完成度
- Phase 0/1/2/8 ✅ 完成。
- Phase 3 🟡 abstraction + /context 完成;**剩余 cutover**（见下「继续指南」）。
- Phase 4 🟡 read_file caps 完成;**剩余 tree-sitter repo map**（需加依赖 tree-sitter + grammars）。
- Phase 5 🟡 typed profile/registry/permission + capabilities(PermissionContext/taxonomy) 完成;
  **剩余 engine._execute_tool_call 改经 CapabilityRouter 派发**。
- Phase 6 🟡 ResultEnvelope + **spawn slices 1+2 完成**(SubAgentRunner: build_sub_agent/写盘/run 原语/
  finalizers 搬到 runtime/spawn.py,engine 保薄委托,~350 行已迁);**剩余 slices**: ① _execute_agent_tool
  (fresh/resume/background 分派,~210 行) ② _spawn_background_subagent/_run_background_subagent(~180 行)
  ③ _spawn_memory_consolidate/eval/optimize + _run_*(~200 行) ④ _execute_skill_tool fork(~90 行)。
  全是 host-driven 搬迁 + engine 薄委托(tests 按名调用,delegate 必留)。保 background pin live leaf
  (engine.py footgun)、双 flock、trajectory_id 不分叉。
- Phase 7 ⬜ 未开始: CLI as runtime client。

### 继续指南（剩余 = 把旧 engine.py 代码迁入新架构;每步小颗粒 + 测试 + 提交,保持全绿）
**性质转变**: 已完成的是「建新架构」(additive,干净);剩余是「迁旧码入新家」(destructive,改 model-visible
行为/重测试耦合)。务必小步 + 频繁提交。

1. **Phase 5 router reroute**: engine._execute_tool_call 改为 `classify_capability` 分派 + `router_allowlist_blocks`
   咽喉点。保 3 个 fail-closed 点（_execute_tool_call/run_shell 后台/_run_hook）。安全回归测试
   (test_subagent_security_regression/caps/callgate) 必须全绿不放松。PermissionEngine(self) 可改 PermissionContext。
2. **Phase 6 spawn_child**: engine.py 1055-2125（_execute_agent_tool/_build_sub_agent/_spawn+_run_background/
   _finalize_*/_await_subagent_run）→ runtime/spawn.py + spawn_policy.py + capabilities/subagents.py adapter。
   用 agents/result.ResultEnvelope。**保**: lease 隔离、resume 双 flock 守卫(engine.py:1980)、超时 vs 完成判别
   (_await_subagent_run pending-set)、background completion pin **live** leaf(非 spawn leaf,engine.py:846-848 footgun)、
   权限继承(agents/permissions.derive_child_profile)、`session_id==_tree_session_id 共享 trajectory_id` 不变量
   (engine.py:1090,独立 child sid 会分叉 trajectory_id 除非显式透传)、消除 artifact_id=='main' no-lease 洞。
3. **Phase 3 cutover**: build_system_prompt 把 {{git_context}}/{{claude_md}}/{{memory}} 置空(留 cwd/date/
   platform/shell/skills/agents/deferred),改由 session-start custom_message packs 注入。
   **⚠ resume-dedup gotcha**: 不能用进程内 flag dedup —— resume 时树里已有这些 pack,会重复注入。
   必须按 customType 查树是否已注入,或仅在 session **创建**(非 resume)时注入 + compaction 后按 survival matrix
   重注入。**需更新 e2e 测试**: test_p2_engine_e2e 的「clean tree / msgs[0]=='hello'」断言会因注入 git/项目指令
   pack 而失效(测试 repo 有 git)。删 test_cutover_characterization.py(只钉要删的 append_to_last_user/_inject_* flat)。
4. **Phase 7 CLI as runtime client**: RuntimeThread.events() 驱动 CLI 输出;删私有 core 访问
   (cli 的 agent._load_messages/_system_prompt/_session_mgr 等)、删 NANOCODE_REPL_VIA_RUNTIME=0 逃生门、
   approvals 带 thread/agent 身份。保 SIGINT-restore-in-finally + fail-closed can_switch()。
5. **Phase 4 repo map**: 加 tree-sitter 依赖 → codeintel/{symbols(填充),index,graph,repomap,cache}.py →
   RepoMapProvider 接入 ContextRuntime。green-field 可并行。



- python 入口: `.venv/bin/python`(无全局 python)。
- codex reviewer: `codex` 0.139.0 + `NEO_TOKEN` 已设(交叉验证用)。
- 硬不变量(§0): `AgentCore.state` 只能是 `session.jsonl` 的**可丢弃投影**,SessionManager 是唯一 durable truth。

## 现状关键事实(已第一手核对源码)
- `session/{tree,manager,context,render,capture,lease}.py` **已基本做对**,保留并上移为 L3 truth。
  管线: `get_branch(leaf) → context.fold → convert_to_llm → render(ModelCtx) → provider payload`。
  逆向: `capture.capture_*` (provider msg → 中立 Message)。`build_context()→BuiltContext(messages, scalar)`。
- `agent/engine.py`(2195 行 `Agent` god-class = AnthropicBackendMixin+OpenAIBackendMixin+PlanModeMixin)是事实中心,必须拆解消失。
- 模型循环真正在 backend mixin(`_chat_anthropic`/`_chat_openai`),直接 append/覆盖 `self._{provider}_messages`。
- `_{provider}_messages` 已是每轮从 `_build_request_messages()`(=build_context+render)重投影的 turn-local 列表,但 loop 仍直接 mutate + 当请求源传给 SDK。
- `prompt.py:build_system_prompt()` 把 cwd/date/platform/shell/git/claude_md/memory/skills/agents/deferred_tools 全拼进 system(Phase 3 要拆)。
- `read_file.py` 无 line/byte cap、无 range(Phase 4 要加;`shared._truncate_result` 50k 未接入 read_file)。
- `subagents/config.py` dict 配置(Phase 5 → typed AgentProfile)。
- `agent/session.py` AgentSession 是 58 行薄包装(Phase 2 升级为 state↔tree 同步边界)。
- `runtime_events.py` RuntimeEvent 单流 + DURABLE_TYPES/DURABLE_EVENT_FIELDS(trajectory 从树派生,additive 契约不可破)。

## 推荐落地顺序(依赖修正版,优先于报告 Phase 编号)

- [ ] **STEP A (Phase 0)**: 纯 additive schemas + tests first。新增 `agent/state.py` `agent/events.py`
  `context/packs.py` `context/ledger.py` `agents/profile.py` `codeintel/symbols.py(占位)`。
  契约测试: 提升 `test_p1_render.py→test_render_legality`、`test_p2_equivalence.py→test_rebuild_state`;
  新增 `tests/context/test_ledger.py`+`test_prompt_cache_policy.py`(packs append 不改写 user 消息)。**全绿,零行为变化。**
- [ ] **STEP B (Phase 1)**: providers seam。`agent/providers.py`(ProviderAdapter)**包裹**(不删)现有
  `_call_anthropic_stream`/`_call_openai_stream`+models.py+`_with_retry`。先用 characterization 测试钉住两后端行为。
- [ ] **STEP C (Phase 1)**: AgentCore + loop。`agent/core.py`+`agent/loop.py`,发 typed AgentEvent,abort/steer/follow_up。
  **关键: 精确移植 cancel 不变量**(吞 CancelledError→_aborted、每个 `if self._aborted: break` poll 点、
  `_await_subagent_run` pending-set 超时判定)。旧路径 flag-gated 并行直到验证通过,再从 MRO 删 BackendMixin。
- [ ] **STEP D (Phase 2)**: AgentSession 拥有持久化。把 `_tree_record/_tree_event/_tree_custom_message`/
  `_build_request_messages` render seam/compaction-as-entry 移进 `session/agent_session.py`
  (record_event/hydrate_state/run_turn/compact)。**删 flat 请求权威前**先令 assistant/tool_result 树写 required +
  加 turn-end 一致性检查(§7.6)。重发**全部**遥测 emit 点(LLM_REQUEST/TURN_END/TOOL_BLOCKED/PERMISSION_DECISION/BUDGET_EXCEEDED)。
- [ ] **STEP E (Phase 3)**: ContextRuntime。拆 `prompt.py` → 稳定模板 + ContextProviders;所有注入走
  ContextRuntime→record_event 的 custom_message packs。先关掉 `artifact_id=='main'` no-lease 漏洞(从 Phase 6 提前),
  确认所有 subagent tree-backed,再删 flat 注入 fallback + `test_cutover_characterization.py`。加 `/context`(ContextLedger)。
- [ ] **STEP F (Phase 5,先于 6)**: CapabilityRouter + permissions + profile/registry。`capabilities/router.py`+
  `capabilities/permissions.py`(immutable PermissionContext)+`agents/{profile,registry,permissions}.py`。
  **保持单一 allowlist 咽喉点 + check_permission 优先级 + 安全回归测试全绿**。
- [ ] **STEP G (Phase 6)**: runtime spawn child。把 engine.py ~1100 行(`_execute_agent_tool`/`_build_sub_agent`/
  `_spawn/_run_background_subagent`/`_finalize_*`/`_write_agent_*`/`_await_subagent_run`)移到 `runtime/spawn.py`+
  `spawn_policy.py`+`capabilities/subagents.py` adapter;`agent_result.py→agents/result.py` typed ResultEnvelope。
  **保: lease 隔离、resume 双 flock 守卫、超时 vs 完成判别、background completion pin 到 live leaf、权限继承、trajectory_id 不分叉。**
- [ ] **STEP H (Phase 4,可并行/延后)**: RepoIndex。先加 `read_file` byte/line cap + range(additive),再 tree-sitter
  symbols/index/repomap 作 ContextProvider。与 durable-truth 无关,STEP E 后任意并行。
- [ ] **STEP I (Phase 7)**: CLI as runtime client。`RuntimeThread.events()`(保 streaming/spinner/final-response/阻塞审批);
  CommandContext.agent shim → typed handles;approvals 带 thread/agent 身份;删 `NANOCODE_REPL_VIA_RUNTIME=0` 逃生门 + CLI 私有 core 访问。
- [ ] **STEP J (Phase 8)**: TeamRuntime 骨架。`runtime/teams.py` 接口 + 预留 typed-custom session entries
  (team_start/team_task_update/team_message/team_claim/team_result/agent_mailbox_message)。green-field,最后。

## 删除清单(报告 §14,带定位)
- backend `self._{provider}_messages = _build_request_messages()` assign-back + 循环内 `.append` + 传 SDK 作 messages 权威。
- backend memory/skill/task 的 in-place last-user append fallback + `skills/listing.py:append_to_last_user` + engine `_inject_*` flat 分支。
- `_compact_anthropic/_compact_openai` 重建 flat list(改为 tree-entry-as-source,保两区 fold+firstKept)。
- `plan_mode.py` `_openai_messages[0]` system 重写 + `_clear_history_keep_system` + 循环里 `_context_cleared` flag。
- engine `_load_messages/_dump_messages/_replace_messages/_active_messages/_append_message` 列表 ownership shim + cli/session 对其调用。
- engine `_persist_state`/`_reload_task_state` 作 resume 权威 + cli `_session_v2.read_state` adopt(改为从 canonical child tree 重建;TaskManager 降为 derived cache)。
- `tools/registry.py` 模块级 `_activated_tools` 全局集(改为 per-AgentState/thread + session entry)。
- `prompt.py:build_system_prompt()` 作主上下文源(只留稳定身份/行为文本)。
- `subagents/config.py:get_sub_agent_config` dict API 作长期 profile API(→ typed AgentProfile;保 _resolve_effective 收窄代数 + trust gate + _filter_tools)。
- engine.py 1055-2125 subagent 机器(移 runtime/spawn.py)+ 消除 `artifact_id=='main'` no-lease 洞。
- runtime/CLI 伸进 Agent 私有: `agent._session_mgr`/`agent._sink`/`agent._aborted`/`_load_messages`/`_reload_task_state`/`_get_message_count` 等(改 public API)。
- `cli.py` `NANOCODE_REPL_VIA_RUNTIME=0` 逃生门。
- `agent/engine.py` 本身作架构中心(Phase 1-7 完成后的最终产物)。

## 头号风险(全程警戒)
1. **Cancel/abort 正确性**横跨整个重构: 吞 CancelledError→正常返回 的语义 + 每个 poll 点 + pending-set 超时判定。丢任一处 → 超时/abort 被误当 success。STEP C 用 flag-gated 旧路径并行 + 显式 cancel characterization 测试缓解。
2. **删 flat 请求权威** → 每个请求往返树且**无 fallback**。**精确定位**: `_tree_record` 对 assistant/toolResult 默认 best-effort(`required=False`,engine.py:663-664),仅 user 消息 `required=True` 重抛。删 flat 后 assistant/toolResult 树写静默失败 → leaf 指向更早 entry → 下轮 build_context 静默丢这条消息。STEP D 必须: (a) 令 assistant/toolResult 树写 **required** fail-loud; (b) turn-end 校验 5 条(§7.6): 每个 assistant toolCall 有 toolResult-or-synthetic entry(非仅 render-time 合成,树里别留永久 forward-orphan)、aborted assistant 标 stopReason=aborted/error、无永久 inverse-orphan toolResult entry、leaf==本 turn 最后写入的 leaf-affecting entry id、firstKeptEntryId 在 branch 内可达。注: render Pass A 已在**渲染时**兜底孤儿,但树里仍可能留永久孤儿污染 trajectory。
3. **P4 fail-closed allowlist** 是受限/只读 subagent 的安全脊柱,今天在 3 个咽喉点(`_execute_tool_call`/run_shell 后台分支/`_run_hook`)。拆 CapabilityRouter 时任何新路径(MCP/hook shell/skill-fork)逃逸 → 只读 subagent 重获 shell。安全回归测试必须全绿/不放松。
4. **subagent 抽取 ~1100 行**密集脆弱不变量: lease 隔离、resume 双 flock 守卫、超时 vs 完成、background completion pin 到 **live** leaf(非 spawn leaf,否则 fork 不可见 sibling 又 fork 用户下轮)、权限继承(child≤parent、deny-union/allow-intersect)、`session_id==_tree_session_id 共享 trajectory_id` 不变量(独立 child sid 会分叉 trajectory_id 除非显式透传 lineage)。
5. **测试安全网倒置 + 并发 worktree**: `test_cutover_characterization.py`/`test_skill_injection.py` 守护的正是要删的行为 → 删它们前必须先让新 ContextRuntime pack 测试落地(STEP A)。多 session 共享同一 worktree(见 memory),用 targeted per-module 子集验证,绝不盲目全量 revert;conftest fd-limit + cache-reset autouse fixture 是 load-bearing。
6. **Trajectory 派生**(三层边界 tree=facts / metrics·evals=labels-never-in-tree)依赖散落的遥测 entry。AgentEvent→session-entry 转换必须重发全部且 DURABLE_EVENT_FIELDS 保持 additive,否则 trajectory export 静默丢观测。
7. **拆 prompt.py 的缓存稳定性双向回归**: system 今天构造一次复用整 session(项目指令/memory "免费"survive compaction)。移成 per-turn pack 若 lifecycle 误判会 (a) 降缓存命中,(b) compaction 后静默丢项目指令/memory,除非 §8.4 survival matrix 显式 reload。plan-mode 改 pack/mode 事件须保留模型仍看到 plan-<sid>.md 路径。

## 新增模块(按依赖序,报告 §5 + 综合)
state.py / events.py / context/packs.py / context/ledger.py / agents/profile.py → providers.py / loop.py / core.py →
context/{providers,budgets,cache_policy,runtime}.py → session/agent_session.py → capabilities/{permissions,router,subagents}.py +
agents/{registry,permissions,result}.py → runtime/{spawn,spawn_policy,thread,runtime,approvals,events}.py →
codeintel/{symbols,index,graph,repomap,cache}.py → runtime/teams.py。

## 测试 blast radius
~26/126 文件需重写/删(engine 内部耦合: ~14 断言 `_anthropic_messages`,~12 触 `_inject_*`/`_spawn_*`/`_session_mgr`)。
~100/126 provider-neutral,至多改 import path。比例 ~1:4。新建目录: tests/{codeintel,context,agents,runtime}/。
**删除: `test_cutover_characterization.py`(只为钉 append_to_last_user + 三个 _inject_ flat 方法)——但必须在新 pack 测试落地后。**
监控: monkeypatch 目标随模块移动(subagents.config._project_agents_trusted→agents/registry;reset_*→capabilities/)会 AttributeError 伪装成逻辑回归。
