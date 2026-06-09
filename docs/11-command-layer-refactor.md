# 11 · Command 层改造(修订版)

> 本文是对早期「REPL slash command → command/runtime 层」改造草案的修订版。
> 草案的方向(用一个 registry 统一 dispatch + 补全)是对的、诊断也属实,但有三处会改变前提的问题:
> (1) 把 trace 误称为 argparse 子命令;(2) 把 runtime/session 底座当成 greenfield,实际上 `AgentSession`/`AgentRuntime` 等**已经存在**;(3) 把一个早已写在 `docs/09` 里的终态当成新主张。
> 本文修正这三点,补齐草案缺失的不变量、测试策略、失败隔离与若干开放设计点,并把阶段重命名为 `CMD-P*` 以避免与现存编号冲突。

---

## 结论

`nanocode` 应该把「在 `run_repl()` 里用 `if/startswith` 串行分发 slash command」收敛为「CLI、REPL slash command、未来 RPC/SDK 共用一套 command client 层」。

但必须明确两点定位:

1. **command 层是 `docs/09` 已经规定的「slash command client」,是 CLI-client 关注点,不是新的核心层。** 它在既有 runtime/session 之上,不与之平行。
2. **runtime/session 底座已经落地**(见下),所以本次真正的活比草案小得多:核心是让 `run_repl` 不再直接 reach 进 Agent 私有面、并最终走 `AgentRuntime`,而不是「引入 Pi-style runtime」。

短期先做 `commands/` 抽取 + 补全统一 + `/trace` 复用;中期再把 lifecycle(`/new`、`/fork`、`/resume`、`/model`)逐条接到既有 runtime 上。

---

## 当前现状(已对照代码核实)

### slash command 分发

REPL 主循环 `run_repl(agent)` 在 `src/nanocode/entrypoints/cli.py:473`(全函数约 250 行,dispatch 段 `562-721` 约 160 行)。slash command 通过一长串 `if inp == ...` / `inp.startswith(...)` 串行分发,每个分支以 `continue` 收尾,**没有任何 registry/字典分发**。

实际命令远多于草案列举的 5 个,且 dispatch 风格混杂、顺序 load-bearing:

| 输入 | 行 | 匹配方式 | 触碰的状态 |
|---|---|---|---|
| `exit` / `quit` | 564 | 裸词 `in (...)` | 跳出循环;故意不进补全菜单(`42-43`) |
| `!<cmd>` | 570 | `startswith("!")` | `_run_user_shell`(`233-254`),**故意绕过权限系统**,仅 print |
| `/clear` | 577 | 精确 | `agent.clear_history()`(改会话+token计数+skill缓存) |
| `/plan` | 580 | 精确 | `agent.toggle_plan_mode()` |
| `/cost` | 583 | 精确 | `agent.show_cost()`,只读 |
| `/compact` | 586 | 精确(已 try/except) | `await agent.compact()`,改消息历史 |
| `/memory consolidate` | 592 | 精确 | `agent._spawn_memory_consolidate()`(私有) |
| `/memory eval generate` | 596 | 精确 | `agent._spawn_memory_eval()`(私有) |
| `/memory optimize` | 600 | 精确 | `agent._spawn_memory_optimize()`(私有) |
| `/memory eval [...]` | 604 | 精确 OR `startswith` | `handle_eval_command` → 改 `eval_store` **模块单例** |
| `/memory` | 608 | 精确 | `list_memories()`,只读 |
| `/skills` | 617 | 精确 | `discover_skills()`,只读 |
| `/sandbox [k v]` | 628 | 精确 OR `startswith` | 读/写 `sandbox_defaults` **模块单例** |
| `/tasks [status]` | 647 | 精确 OR `startswith` | 读 `agent.task_manager` |
| `/task-stop <id>` | 652 | `startswith`,无裸形 | 改任务状态,触 `agent._background_tasks`(私有) |
| `/task <id>` | 657 | `startswith`,无裸形 | 只读 `agent.task_manager` |
| `/agents [...]` | 662 | 精确 OR `startswith` | 读 `agent.task_manager` + `agent.session_id` |
| `/agent <id>` | 687 | `startswith`,无裸形 | 只读,触 `agent.session_id` |
| `/<skill> [args]` | 694 | **泛 `startswith("/")` catch-all** | `execute_skill` / `agent._register_skill_hooks`(私有)+ `session.run_turn` |
| 普通 chat | 716 | fall through | `await session.run_turn(inp)` |

**三处 load-bearing 行为**(任何「首 token → handler」字典都会静默破坏):

- **分支顺序即语义(most-specific-first)**:`/memory consolidate|eval generate|optimize`(592–602) → `/memory eval`(604) → 裸 `/memory`(608);`/task-stop` → `/task` → `/` catch-all。
- **未知 `/foo` 落空 → chat**:`694` 的 skill 块在 `get_skill_by_name` 落空时**不 `continue`**,fall through 到 `716`,把 `/foo` 当普通文本发给模型。这是必须保留的行为。
- **全角 `／→/` 归一化**(`558-559`)是命令解析的一部分。

**已有但分离的两份元数据**(已经漂移,正是 registry 要消灭的):

- `_BUILTIN_COMMANDS`(`44-57`):静态 12 项 `(name, desc)`,**仅供补全**,与 dispatch 链无关,已漂移(列了 `/memory` 但没有它的子命令)。
- `_CommandCompleter`(`60-81`):消费上表 + user-invocable skills。
- `--help` 文本(`756-777`):**第三份**手维护副本。

### 顶层 `trace` 子命令(修正:不是 argparse 子命令)

`_SUBCOMMANDS = {"trace": _run_trace_cmd}` 在 `cli.py:34`,但 `parse_args()`(`401-426`)**没有注册任何 subparser**。trace 是 `main()` 里 `cli.py:726-727` 的**手写 `argv[0]` 查表,在 `parse_args()`(`729`)之前短路**。handler 形如 `run(argv: list[str]) -> int`。

`trace_cmd.run`(`src/nanocode/entrypoints/trace_cmd.py:10`)形状确实可复用,但有三个 REPL 隐患:
- 内部 `argparse` 默认 `add_help=True`,`parse_args()`(`trace_cmd.py:28`)对 `-h` 抛 `SystemExit(0)`、坏参抛 `SystemExit(2)` —— **未捕获会杀掉 REPL 进程**。
- 输出走 `console.print`/`print_error`,只返回 `int`,REPL **无法捕获/改写输出**。
- 非 wire 路径 `trace_dir()` 绑 `Path.cwd()` 且有 `mkdir` 副作用(`config.py:20-21`)。
- `run()` 是**同步**的,无需 `await`。

### 关键:runtime/session 底座已经存在

`src/nanocode/agent/` 下已落地并由 `__init__.py` 导出:

- `session.py` —— `AgentSession.run_turn / resume / fork_to`、`SessionContextBuilder`
- `runtime.py` —— `AgentRuntime`、`RuntimeThread`、`TurnResult`、`AgentResult`、`ApprovalManager`
- `context_builder.py` —— 事件树 leaf→root 重建(`docs/09` 的 P5)
- 测试:`tests/agent/test_runtime.py`、`test_p5_integration.py`

**因此草案的 P3/P4「引入 Pi-style runtime/session」基本已完成。** 真正剩余的窄口子是:`run_repl` 仍在 `cli.py:477` **inline 构造** `session = AgentSession(agent)`,且 cli.py 里**完全没有** `AgentRuntime`/`.adopt()`/`RuntimeThread` —— 即 REPL 尚未走 runtime facade,而是直接 reach 进 `agent.clear_history` / `agent._spawn_*` / `agent._background_tasks` 等私有面(`cli.py:577-714`)。

---

## 与 docs/09 的关系(本次定位)

`docs/09:771-797` 已**几乎逐字**规定了草案的「最终目标」:`cli.py` 只承担 argument parsing / REPL input / **slash command client** / event rendering / approval UI / runtime bootstrap;不再 直接造 Agent / restore session / 读写 task·subagent 状态 / 实现 approval 业务 / 渲染核心 print。

由此得到三条约束:

1. command 层填的是 docs/09 的「slash command client」格子,**是 CLI 关注点,不是核心层**——这正好支持把 `commands/` 放在 entrypoints 侧。
2. 结果对象已有 `TurnResult`/`AgentResult`;产生 turn 的命令应**复用**它们,不要发明平行结果。
3. `docs/09:687` 把 cancel/approval 列为**不可回归契约**:cancel 必须 delegate `agent.abort()`;`chat()` 把取消吞成 `_aborted=True` 正常返回,所以状态要在 await **之后**读 `_aborted`(正常返回 ≠ 成功)。command 层不得重新实现这套。
4. `docs/09` P0.5 的 **PermissionEngine 是唯一 fail-closed 咽喉**,所有入口必须复用;App Server / JSON-RPC / SDK 被标为 aspirational、以「出现第二个 client」为前置。

---

## 参考系统发现(修正若干失真)

- **Claude Code** 命令是 `prompt | local | local-jsx` 的**判别联合**,共享 `CommandBase`(`description/aliases/isEnabled/isHidden/argumentHint/userFacingName`)。注意:`supportsNonInteractive` **只在 `local` variant**(`command.ts:74-78`),`prompt` 用 `disableNonInteractive`;启用是**两层正交**——静态 `availability`(auth/provider 门)vs 动态 `isEnabled()`。结果对象 `ProcessUserInputBaseResult`(`messages[] / shouldQuery / resultText / nextInput / submitNextInput / allowedTools? / model? / effort?`)用**一个 type tag** 决定「是否产生模型可见消息 / 是否 query」,矛盾态不可表示。`QueryEngine.submitMessage`(async generator)owns 整个 query 生命周期,REPL 只是调用方之一。
- **Codex** 把 slash command 做成带元数据的枚举(描述/别名/是否支持 inline args/可否任务中执行)。
- **Aider** 最轻量:`cmd_xxx()` 自动成 `/xxx`,`!cmd` 映射 `/run`,生命周期变化用抛 `SwitchCoder` 让外层重建。
- **Pi** 的重点是 session/RPC runtime,fork/resume/new 是 runtime 能力——这与「不从 REPL 改 Agent 内部」一致,而该能力 nanocode 已具备(`AgentSession.fork_to/resume`)。

---

## 建议架构

```text
src/nanocode/entrypoints/commands/   # slash command client(CLI 关注点)
  types.py        # CommandSpec / CommandContext / CommandResult
  registry.py     # 注册 + most-specific-first 查找
  runner.py       # 解析 + 失败隔离 + 分发,返回 CommandResult
  builtin/        # clear / plan / cost / compact / memory / skills /
                  # sandbox / tasks / agents / trace / status / help ...
```

### CommandResult —— tagged union,不是平铺布尔

草案的七字段平铺(`handled + output + message_to_agent + should_query + exit_repl + replace_agent + next_input`)能表达矛盾态:`message_to_agent` 与 `should_query` 编码的是**同一个事实**。改为判别联合:

```text
Local(output: str | None, exit_repl: bool = False)
    纯本地副作用,从不 query。覆盖第一批几乎所有命令。
Prompt(prompt)
    产生一个 turn —— 必须走 RuntimeThread.run → TurnResult,
    不得自行实现 turn/cancel/approval/token 计账。
Control(...)            # CMD-P3 再加:replace_thread / switch_runtime 等
    仅承载没有 TurnResult 对应物的控制流信号。
```

- `should_query` 从 variant 推导,不做独立字段。
- **不要 `handled` 字段**:`registry.lookup(inp) -> Command | None`,`None` 即「不是命令」,交回 chat —— 这才是 nanocode 必须保留的「未知 `/foo` → 模型」行为(与 Claude Code 不同,这里确实需要一个落空哨兵,但它在 dispatch 边界,不在结果结构里)。
- per-command 的 `model/effort/allowedTools` override、输出通道(user-visible / model-visible / hidden)是 Claude Code 有而本设计暂缺的槽位:**CMD-P0/P1 显式不做**,留待需要时加到对应 variant。

### CommandSpec —— 加判别式,砍投机字段

```text
CommandSpec:
  name
  aliases
  description
  arg_hint
  is_hidden
  is_enabled
  kind            # local | prompt | control —— 最 load-bearing,草案缺失
  source          # builtin | user | project —— 仅当配套 loader 时才有意义
```

砍掉草案的:
- `available_during_turn` —— REPL 严格串行(`535-721` 读一行→跑完→再画提示符),无并发输入通道,这是**没有机制的死元数据**。
- `requires_agent` —— 恒 True。
- `mutates_session` —— 几乎恒 True 且语义模糊;若确需,要区分「改 Agent/会话状态」(clear/plan/compact/skill-hook)与「改进程级单例」(`sandbox_defaults`、`eval_store`,事件存储不重放)。
- `supports_inline_args` —— 把 per-variant 能力拍平;`is_enabled` 同理合并了 Claude Code 正交的 `availability` vs `isEnabled()`。第一批不需要这种保真度,代码真读它再加。

### CommandContext —— 同时带 agent 与 session,标注为待收缩的 shim

```text
CommandContext:
  agent           # 当前为 shim:handler 仍要 reach Agent 私有面
  session         # 必须复用 run_repl 现有的 AgentSession(cli.py:477),不得新建
  out             # 输出 sink(见「print→EventSink」)
  cwd
  settings
```

handler 是对 `agent`(参数)**和**局部 `session`(`477`)**双重闭包**且带 print 副作用,所以两者都要穿进去。新建 `AgentSession` 会换掉 `SessionContextBuilder` 身份,是静默行为变更。把 `agent` 这条宽引用**显式标为 shim**,目标是随 `docs/09` P-1 解耦逐步收缩。

---

## 命令分类映射(供 CMD-P0 落地)

- **Local(纯副作用,不 query)**:`/clear` `/plan` `/cost` `/compact` `/memory*` `/skills` `/sandbox` `/tasks` `/task` `/task-stop` `/agents` `/agent` `/trace` `/status` `/help`
- **Prompt(产生 turn)**:`/<skill>` 调用、普通 chat
- **裸词 / 非 slash(由 runner 在 slash 分发前特判)**:`exit`/`quit`、`!<shell>`(保留其权限绕过语义,不得静默并入 PermissionEngine 门)

---

## 不变量(CMD-P0 验收标准)

CMD-P0 = **逐字 1:1 搬迁,不做任何合并**。以下作为硬性验收:

1. **most-specific-first**:registry 查找顺序等于今天的源码顺序;`/memory eval generate` 绝不被 `/memory eval` 分支吃掉。
2. **未知 `/foo` → chat**:`lookup` 返回 `None` → 走 `session.run_turn`,原样发给模型。
3. **双闭包**:`CommandContext` 同时携带 `agent` 与 **现有** `session`;不新建 `AgentSession`。
4. **全角 `／` 归一化**(`558-559`)随 dispatch 一起搬进 runner。
5. **裸词命令**:`exit/quit`、`!shell` 在 slash 分发之前特判;`!shell` 保留权限绕过且**不进**补全菜单。
6. **不折叠补全**:`_BUILTIN_COMMANDS` 的合并留到 CMD-P1(否则「纯搬迁」变成「搬迁+去重+消漂移」,无法按 no-op diff 审查)。

---

## 测试策略(草案完全缺失;`run_repl` 当前零覆盖)

109 个测试文件无一驱动 `run_repl`(只覆盖 `handle_eval_command` 等纯函数与 completer)。对未测 dispatch 做改写风险高。把「**characterization 套件在 diff 两侧都绿**」设为 CMD-P0 的 merge gate:

1. **表驱动分发测试**:喂 `/clear`、`/memory eval`、`/memory eval generate`、`/sandbox a b`、`/task-stop X`、`/agents show Y`、`/unknownskill`、`普通文本`、`!ls`、`／memory`(全角)、`exit`,断言「哪个 handler 触发 / 落 chat / 落 shell」。
2. **golden-transcript**:用假 Agent(其 `clear_history`/`show_cost`/… 记录调用)断言调用序列与打印文本;借 `sink.py` 的 Null/Buffer sink 录输出。
3. **顺序不变量**直接断言:`/memory eval generate` 不被 `/memory eval` 命中;`/unknown` 被原样发给模型。

---

## runner 失败隔离契约(草案最大遗漏)

草案只为 `/trace` 单点提了 `SystemExit`,没泛化。集中 dispatch 却不集中失败处理,**比现状更糟**。runner 包住每次 handler 调用:

- catch `SystemExit`(任何基于 argparse 的 handler,如 trace)→ 吞掉,至多打印用法。
- catch `Exception` → `print_error(str(e))` 并继续循环。
- 只重抛 `KeyboardInterrupt` / `asyncio.CancelledError`,让 Ctrl-C 与 turn 取消正常传播。
- 把 `cli.py:712,720` 那个靠 `"abort"` 子串判断的脆弱逻辑换成 typed `AbortError`。
- 解析层:对 `shlex.split` 的未闭合引号 `ValueError`(如 `/trace 'foo`)单独捕获——它**不是** `SystemExit`。

---

## 改造阶段(CMD-P0 … CMD-P4)

> 命名空间前缀 `CMD-` 是必须的:仓库已有三套 `P0-Pn`——`docs/09` 的 `P-1..P8`、`agent-cli-...-roadmap.md` 的 `P0..P6`、`devlog/2026-06-08-subagent-p1-p4-wip.md`(即 `feat/subagent-p1-p4` 分支)。裸 `P0-P4` 会静默冲突。

- **CMD-P0 · 纯抽取**:先写 characterization 套件;把 dispatch 链逐字搬进 `commands/builtin/*`,`run_repl` 只读输入→`runner.dispatch()`→否则 chat。满足上面 6 条不变量 + runner 失败隔离。`_SUBCOMMANDS` 的 argv[0] 短路保持原样,**不要**折进 argparse subparser。
- **CMD-P1 · 补全 + /help(提前!)**:registry 成为 dispatch / 补全 / `--help` 的**唯一来源**,删掉 `_BUILTIN_COMMANDS`(`44-57`)与 `--help` 手写块(`756-777`)两份副本,消除现存漂移。这是最低风险、最能验证 registry 的一步,所以排在 trace 之前(纠正草案把高隐患的 trace 放 P1 的风险倒挂)。补全要支持子命令(`/memory <TAB>`)。
- **CMD-P2 · /trace 复用(硬化)**:`try: _run_trace_cmd(shlex.split(rest)) except SystemExit: pass`;`shlex.split` 的 `ValueError` 另捕获;pin `NANOCODE_TRACE_DIR` 避免绑 launch-cwd 与 mkdir;输出限定终端(暂不捕获字符串);`run()` 同步,无 `await`。加测试:`/trace -h`、`/trace --bogus`、`/trace 'foo` 都不得杀 REPL。
- **CMD-P2.5 · 让 REPL 走 runtime**:把 `run_repl` 的普通 chat / skill turn 从直接 `session.run_turn` 改为经 `RuntimeThread.run`(底座已存在),使后续 lifecycle 有真正可返回的 runtime。`Prompt` 结果在此接入 `TurnResult` 与 cancel/`_aborted`/approval 契约。
- **CMD-P3 · lifecycle,逐条拆开**(不要一锅端):
  - 3a:引入 `Control` plumbing,接 **一个** 低风险消费者(`/new` 或 `/fork`,走已有 `AgentSession.fork_to`——它对非法 `from_event_id` 抛 `ValueError`、**不得静默清历史**)。
  - 3b:`/resume` 复用 `restore_session` 的 events-authoritative + snapshot fallback,无数据丢失。
  - 3c:`/model` 单独一步,或保持 deferred(与「命令边界」的延后清单一致,消除草案自相矛盾)。
  - `replace_agent` + resume + model **绝不**进同一个 PR。
- **CMD-P4 · 向 runtime facade 收敛**:command 层返回进**进程内** `AgentRuntime`/`RuntimeThread`,带工具的命令一律过 PermissionEngine 咽喉。RPC/SDK 共用同一 API 按 `docs/09` 标为 **aspirational**,不列为近期阶段。

---

## 命令边界

**第一批进 registry**:`/clear` `/plan` `/cost` `/compact` `/memory`(及其子命令)`/skills` `/sandbox` `/tasks` `/task` `/task-stop` `/agents` `/agent` `/trace` `/status` `/help`,以及裸词 `exit`/`quit` 与 `!shell` 的特判。

**延后**(等 `Control` variant 成熟):`/api-base`、`/memory-backend`、复杂 `/resume`、跨 provider `/model` —— 这些绑在 Agent 构造 / env·trust / session restore 阶段。

---

## 仍待补的开放设计点

1. **Headless / `-p` 对称性**:one-shot 路径(`cli.py:888-896`)**根本不进 `run_repl``**,所以「CLI 获得 REPL 能力」在结构上未达成。要么 one-shot 首行也过 registry,要么明确把命令限定为交互式并写明;并按 Claude Code 的 per-variant `supportsNonInteractive` 决定哪些命令可在非交互下用。
2. **文件级 / 用户自定义命令**:`source` 字段没有 loader / 优先级 / 重名规则就只是装饰。必须与**已存在的 `.nanocode/skills`**(`discovery.py` 的 `source/context/user_invocable`)对齐,定义「builtin 胜过 skill 胜过 落空→chat」与 `.nanocode/commands/` 的发现根。
3. **PermissionEngine 路由**:带工具的命令(skill 调用)必须过 `docs/09` P0.5 的咽喉;`!shell` 现在绕过权限,command 层**不得静默把这个绕过形式化**。
4. **print → EventSink**:REPL handler 现用裸 `print/print_info/print_error` 而非 `agent._sink`。`docs/09` P2(EventSink 替代核心 print)是「cli.py 只做 output rendering」终态的前提;`CommandContext.out` 应朝 sink 收敛。
5. **bootstrap 解耦(终态真正的卡点)**:`main()`(`724-901`)里 API key、trust gate、resume 解析(必须在 Agent 构造前)、memory-backend、Agent 构造全交织——这是 `docs/09` P-1。command 层不依赖它,但「cli.py 只做 bootstrap」要等它完成。

---

## 最终目标

对齐 `docs/09:771-797`:`entrypoints/cli.py` 最终只承担 argument parsing、REPL input、slash command client 装配、event rendering、approval UI、runtime bootstrap。业务命令、session 操作、task/subagent 查询、trace 查询收敛到 command client + 既有 runtime/session 层。这样 REPL「拥有 CLI 能力」,而 CLI 入口不会变成第二个 Agent 内核——且复用而非重造已经落地的 `AgentSession`/`AgentRuntime`/`TurnResult`/`PermissionEngine`。
