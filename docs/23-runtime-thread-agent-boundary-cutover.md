# 23 · RuntimeThread Raw Agent Boundary Cutover Report

> 目标：在 0 上下文环境下，按本文即可把 nanocode 的嵌入式边界收敛到“内部像 Pi，外部像 Codex/OpenCode”的形态。
>
> 结论先行：`Agent` 保留为内部模型循环与工具执行内核；`AgentSession` 保留为 canonical `session.jsonl` 的 state/tree 同步层；`AgentRuntime`/`RuntimeThread` 成为 CLI/RPC/TUI/SDK 唯一外部控制面。外部 `RuntimeThread` 不再公开 `.agent` / `.session`。

## 1. 背景与判断

nanocode 已经采用 Pi 风格的 canonical session tree：`session.jsonl` 是对话事实源，runtime/session 负责启动、resume、fork、clone、rebind 和 writer lease。当前剩余问题不是重写 runtime，而是把最后的 raw `Agent` 穿透点清掉。

推荐方案：

1. 内部结构对齐 Pi：
   - runtime host 拥有当前 session、cwd-bound services、session replacement。
   - `AgentSession` 包住 low-level agent loop，负责 tree 写入、hydrate、compact、turn shell。
   - `/new`、`/resume`、`/fork`、`/clone` 只经 runtime replacement。

2. 外部边界对齐 Codex/OpenCode：
   - 外部只拿稳定 thread/service handle。
   - prompt、cancel、approval、event、state、messages、shell、task、subagent、session 操作都经 facade。
   - 不把 raw agent 暴露给 CLI/RPC/TUI/embedded clients。

3. nanocode 特有能力继续保留：
   - `SessionLease` 单写者。
   - `PermissionEngine` 和 sandbox policy。
   - canonical child sessions 与 bounded parent-visible subagent result。
   - JSON-able runtime event envelope。

## 2. 非目标

- 不保留 `nanocode.agent.runtime` / `nanocode.agent.session` 旧 import 兼容。
- 不做 legacy v2/flat session fallback。
- 不增加 feature flag 双路径。
- 不把 Pi/OpenCode 的存储格式照搬进 nanocode。
- 不为了白盒测试保留 public `.agent`。

## 3. 参考源码总表

### 3.1 nanocode 当前源码

| 主题 | 源码 | 说明 |
| --- | --- | --- |
| `RuntimeThread` 仍公开 raw agent/session | `src/nanocode/runtime/facade.py:346-350` | `self.agent = agent`、`self.session = session` 是当前最大外部穿透点。 |
| runtime event envelope 已有 | `src/nanocode/runtime/facade.py:117-121`、`src/nanocode/runtime/facade.py:380-400` | `serialize_event_envelope()` 和 `_on_agent_event()` 已把 dataclass event 转 JSON-able envelope。 |
| turn 已经经 facade | `src/nanocode/runtime/facade.py:421-442` | `RuntimeThread.run()` 包装 `AgentSession.run_turn()` 并返回 `TurnResult`。 |
| stale thread 已有 run guard | `src/nanocode/runtime/facade.py:421-425` | disposed thread 调用 `run()` 会 fail loud。 |
| dispose 已摘除 agent event tap | `src/nanocode/runtime/facade.py:450-470` | rebind 后旧 thread 不再继续收 agent 事件。 |
| status 仍读 raw agent 私有字段 | `src/nanocode/runtime/facade.py:488-509` | 这是 facade 内部可接受，但应改为 `_agent` 私有访问。 |
| `can_switch()` 检查不足 | `src/nanocode/runtime/facade.py:698-704` | 只看 turn 和 `_background_tasks`，还应纳入 live subagent/run/queued writer 状态。 |
| task/subagent facade 已有但仍读 agent 私有面 | `src/nanocode/runtime/facade.py:706-777` | 对外 API 方向正确，内部实现需收敛成 private `_agent` 或服务接口。 |
| `!shell` 已在 runtime boundary | `src/nanocode/runtime/facade.py:779-812` | `execute_user_shell()` 通过 runtime event/audit 执行，不走模型工具审批。 |
| skill 调用仍触 agent 私有 hook | `src/nanocode/runtime/facade.py:814-835` | `self.agent._register_skill_hooks(skill)` 应后续收进 runtime skill service。 |
| memory/extension task 仍直接读 agent 私有面 | `src/nanocode/runtime/facade.py:837-924` | 可作为第二阶段服务化，不阻塞先隐藏 public `.agent`。 |
| runtime 仍通过 public `.agent` rebind | `src/nanocode/runtime/facade.py:1030-1071` | `_switch_via_rebind()` 用 `host.current_thread.agent`，需要改为 runtime-private getter。 |
| thread lifecycle 已 runtime-owned | `src/nanocode/runtime/facade.py:1105-1192` | `thread_new/resume/fork/clone` 方向正确，应保留。 |
| `RuntimeHost` 已只调用 thread facade | `src/nanocode/entrypoints/host.py:86-90` | `can_switch()` 不 reach 进 agent，方向正确。 |
| CLI 仍拿 `_thread.agent` | `src/nanocode/entrypoints/cli.py:373-383` | REPL 初始化保存 raw agent，需删除。 |
| CLI 审批直接 attach raw agent | `src/nanocode/entrypoints/cli.py:460-462` | 应改为 `thread.attach_approvals(...)`。 |
| RPC 审批直接 attach raw agent | `src/nanocode/entrypoints/rpc.py:69-79` | rebind 后仍 attach `thread.agent`，应改为 facade。 |
| commands 目标注释已正确 | `src/nanocode/entrypoints/commands/types.py:132-144` | handler 应只经 `RuntimeThread` 稳定面操作。 |
| `/new` command 已只返回 Control | `src/nanocode/entrypoints/commands/builtin.py:311-315` | handler 不碰 live agent，方向正确。 |
| `/fork` 已经使用 readonly session facade | `src/nanocode/entrypoints/commands/builtin.py:449-485` | 只读查询走 `ctx.thread.readonly_session()`，切换走 Control。 |
| `AgentSession` 是 tree 同步边界 | `src/nanocode/session/agent.py:1-20` | 注释明确 `AgentSession` 是唯一高层 tree writer。 |
| `AgentSession.run_turn()` 是 turn shell | `src/nanocode/session/agent.py:61-112` | MCP init、lease prologue、context injection、emit user message、compact、core loop、turn end 都在这里。 |
| `Agent.chat()` 仍公开 | `src/nanocode/agent/engine.py:370-373` | 旧外部 turn 入口，应降级为内部或测试专用。 |
| `Agent._ensure_session_lease()` 仍自取 lease | `src/nanocode/agent/engine.py:482-495` | 与 runtime-owned writer lease 目标冲突；激进 cutover 应删除生产 fallback。 |
| compatibility re-export | `src/nanocode/agent/runtime.py:1-23` | 旧 `nanocode.agent.runtime` 入口，应删除。 |
| compatibility re-export | `src/nanocode/agent/session.py:1-11` | 旧 `nanocode.agent.session` 入口，应删除。 |
| `nanocode.agent` 仍 lazy export runtime/session | `src/nanocode/agent/__init__.py:10-44` | 应只保留 low-level agent exports，不再 re-export runtime/session。 |

### 3.2 Pi 参考源码

Pi 仓库路径：`/Users/jyxc-dz-0101321/Projects/agent-reference-repos/pi`

| 主题 | 源码 | 可借鉴点 |
| --- | --- | --- |
| runtime owns current session + services | `packages/coding-agent/src/core/agent-session-runtime.ts:67-80` | `AgentSessionRuntime` 持有 `_session` 和 `_services`。 |
| session replacement 先 teardown 再 create/apply | `packages/coding-agent/src/core/agent-session-runtime.ts:167-181` | replacement 是 runtime 职责，不是 CLI 或 mode 自己拼。 |
| switchSession | `packages/coding-agent/src/core/agent-session-runtime.ts:193-220` | resume 时 open session manager、校验 cwd、teardown 当前，再 createRuntime。 |
| newSession | `packages/coding-agent/src/core/agent-session-runtime.ts:223-256` | `/new` 经 runtime 创建新 session manager 和 session runtime。 |
| fork | `packages/coding-agent/src/core/agent-session-runtime.ts:259-324` | fork 负责校验 user entry，并切换到新 session。 |
| initial runtime factory | `packages/coding-agent/src/core/agent-session-runtime.ts:400-424` | 初始启动与后续 replacement 复用同一个 runtime factory。 |
| cwd-bound services | `packages/coding-agent/src/core/agent-session-services.ts:31-47` | services 以 cwd/agentDir/settings/resource loader 为输入。 |
| create services | `packages/coding-agent/src/core/agent-session-services.ts:137-170` | 每个 effective cwd 创建一组 coherent services。 |
| `AgentSession` wraps low-level agent | `packages/coding-agent/src/core/agent-session.ts:265-270` | Pi 内部 `AgentSession` 包含 `readonly agent`。 |
| dispose invalidates old context | `packages/coding-agent/src/core/agent-session.ts:728-744` | session replacement 后旧 extension ctx 明确 stale。 |
| prompt via session shell | `packages/coding-agent/src/core/agent-session.ts:947-997` | public prompt 不是裸模型循环，而是 `AgentSession` 方法。 |

Pi 的关键借鉴不是“外部公开 agent”，而是 runtime/session/mode 分层和 replacement 生命周期。Pi 自身 `AgentSession.agent` 比较宽松，但 nanocode 的目标是嵌入式边界更强，所以外部边界应参考 Codex/OpenCode。

### 3.3 Codex 参考源码

Codex 仓库路径：`/Users/jyxc-dz-0101321/Projects/agent-reference-repos/openai-codex`

| 主题 | 源码 | 可借鉴点 |
| --- | --- | --- |
| core session 非公开 | `codex-rs/core/src/lib.rs:17` | `session` 是 `pub(crate)`。 |
| public handle 是 `CodexThread` | `codex-rs/core/src/lib.rs:25-29` | 外部 API 导出 thread handle，不导出 low-level session。 |
| `CodexThread` 持有 internal codex | `codex-rs/core/src/codex_thread.rs:160-166` | raw core 是 `pub(crate)` 字段。 |
| prompt/control 走 `submit(Op)` | `codex-rs/core/src/codex_thread.rs:194-196` | 外部提交 operation，而不是直接调用 agent。 |
| event 走 `next_event()` | `codex-rs/core/src/codex_thread.rs:414-420` | 外部只读事件流和 status。 |
| `Codex` 本体是 queue pair | `codex-rs/core/src/session/mod.rs:391-402` | core interface 是 tx submission + rx event。 |
| submit wraps Submission | `codex-rs/core/src/session/mod.rs:711-760` | runtime 生成 submission id 并发送到内部队列。 |
| ThreadManager owns thread lifecycle | `codex-rs/core/src/thread_manager.rs:174-193` | 创建和管理 threads 是 manager 职责。 |

Codex 是 nanocode 外部 `RuntimeThread` 边界的最佳参考：public handle 不暴露 raw core。

### 3.4 OpenCode 参考源码

OpenCode 仓库路径：`/Users/jyxc-dz-0101321/Projects/agent-reference-repos/opencode`

| 主题 | 源码 | 可借鉴点 |
| --- | --- | --- |
| AppLayer 聚合 runtime services | `packages/opencode/src/effect/app-runtime.ts:55-90` | Agent/Session/Permission/Prompt 等是 service layer，不是客户端直连 agent。 |
| Session service interface | `packages/opencode/src/session/session.ts:461-514` | list/create/fork/messages/children/remove 等通过 service 暴露。 |
| SessionPrompt service interface | `packages/opencode/src/session/prompt.ts:86-93` | prompt/cancel/shell/command 是 prompt service 操作。 |
| HTTP session paths | `packages/opencode/src/server/routes/instance/httpapi/groups/session.ts:78-105` | 外部 API 以 session id + operation 组织。 |
| HTTP handlers use services | `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts:47-60` | handler 注入 services，不向 client 暴露 raw agent。 |
| task tool creates child session | `packages/opencode/src/tool/task.ts:121-150` | subagent UX 可借鉴 parent-child session 和 bounded result。 |

OpenCode 的启发是 API/service 化和 child-session UX，而不是把 OpenCode 的 SQLite/HTTP server 作为 nanocode 的权威层。

## 4. 目标架构

### 4.1 分层

```text
External callers
  CLI / RPC / TUI / embedded SDK
    |
    v
Runtime facade
  AgentRuntime
  RuntimeHost
  RuntimeThread
  RuntimeServices
  RuntimeApprovalBroker
    |
    v
Session layer
  AgentSession
  SessionLease
  SessionManager
  canonical session.jsonl
    |
    v
Agent core
  Agent
  AgentCore
  provider backend
  tool loop
```

### 4.2 权限与所有权

| 对象 | Owner | 对外可见性 | 写权限 |
| --- | --- | --- | --- |
| `AgentRuntime` | runtime host | public | 创建/替换 thread |
| `RuntimeThread` | runtime | public | 提供稳定操作；不公开 raw agent |
| `RuntimeServices` | runtime | public readonly/diagnostics | cwd-bound services 由 runtime 重建 |
| `SessionLease` | active RuntimeThread | internal | 唯一 writer lease |
| `AgentSession` | runtime/thread | internal or session package | canonical tree 高层写入 |
| `Agent` | runtime/session internals | internal | 模型循环、工具执行、emit |
| `SessionManager` | `SessionLease`/`AgentSession` | internal/read-only projection | 低层 append/read |

### 4.3 外部 `RuntimeThread` API 最小面

必须保留或新增：

```python
RuntimeThread.thread_id
RuntimeThread.status()
RuntimeThread.state()
RuntimeThread.messages()
RuntimeThread.session_stats()
RuntimeThread.tokens()
RuntimeThread.run(prompt)
RuntimeThread.cancel()
RuntimeThread.wait_for_idle()          # 新增或等价能力
RuntimeThread.subscribe(listener)
RuntimeThread.events()
RuntimeThread.attach_approvals(...)    # 新增
RuntimeThread.readonly_session()
RuntimeThread.set_session_name(name)
RuntimeThread.set_entry_label(entry_id, label)
RuntimeThread.move_to(entry_id)
RuntimeThread.move_to_with_branch_summary(...)
RuntimeThread.execute_user_shell(...)
RuntimeThread.invoke_skill(...)
RuntimeThread.task_list(...)
RuntimeThread.task_output(...)
RuntimeThread.task_stop(...)
RuntimeThread.subagent_widget_snapshot()
RuntimeThread.subagent_conversation_snapshot(child_session_id)
RuntimeThread.subagent_cancel(child_session_id)
RuntimeThread.diagnostics()            # 推荐新增，替代 debug 直摸 agent
```

不得对外公开：

```python
RuntimeThread.agent
RuntimeThread.session
RuntimeThread._agent
RuntimeThread._session
Agent._session_mgr
Agent._background_tasks
Agent.task_manager
Agent.agent_session
SessionManager.append_*
```

## 5. 改造阶段

### Phase 0：建立边界测试

先写测试，避免改完又被旧调用点带回来。

新增或修改测试建议：

| 测试 | 断言 |
| --- | --- |
| `tests/runtime/test_runtime_thread_boundary.py` | `RuntimeThread` 无 public `.agent` / `.session`。 |
| `tests/entrypoints/test_runtime_facade_usage.py` | `src/nanocode/entrypoints/**/*.py`、`src/nanocode/tui/**/*.py` 不出现 `thread.agent` / `current_thread.agent`。 |
| `tests/entrypoints/test_command_import_boundaries.py` | `import nanocode.agent` 不导入 `nanocode.runtime`、provider SDK、`yaml`。 |
| `tests/runtime/test_thread_lifecycle.py` | rebind 后旧 thread `run()` fail loud，新 thread 可继续写。 |
| `tests/entrypoints/test_rpc_boundary.py` | RPC rebind 后 approval/subscription 绑定新 thread。 |
| `tests/session/test_session_lock.py` | busy/corrupt session fail-closed 且不泄漏 lease。 |

可用静态 grep 辅助：

```bash
rg -n "\.agent|agent_session|_session_mgr|_background_tasks|task_manager" \
  src/nanocode/entrypoints src/nanocode/tui

rg -n "from nanocode\.agent import .*Runtime|nanocode\.agent\.runtime|nanocode\.agent\.session" \
  src tests
```

通过标准：

- entrypoints/TUI 不再访问 raw agent。
- tests 中需要白盒构造的地方，改为 `nanocode.runtime` 或专用 test helper。

### Phase 1：新增 facade 方法，不先删除字段

目的：先让所有外部调用点有替代路径。

改动文件：

- `src/nanocode/runtime/facade.py`
- `src/nanocode/entrypoints/cli.py`
- `src/nanocode/entrypoints/rpc.py`
- 相关 tests

建议新增：

```python
class RuntimeThread:
    def attach_approvals(self, *, confirm_fn=None, plan_approval_fn=None) -> None:
        ApprovalManager(
            confirm_fn=confirm_fn,
            plan_approval_fn=plan_approval_fn,
        ).attach(self._agent)

    def wait_for_idle(self) -> None | Awaitable[None]:
        ...

    def diagnostics(self) -> dict:
        ...
```

迁移点：

| 当前代码 | 目标 |
| --- | --- |
| `ApprovalManager(...).attach(thread.agent)` | `thread.attach_approvals(...)` |
| `agent = _thread.agent` in CLI | 删除变量，统一用 `_host.current_thread` |
| RPC rebind 后 `attach(thread.agent)` | rebind 后 `thread.attach_approvals(...)` |
| runtime `_switch_via_rebind()` 读 `host.current_thread.agent` | 用 private runtime helper 取 `_agent` |

注意：

- Phase 1 暂时可以保留 `self.agent`，但外部调用点必须先迁完。
- 不要加兼容 warning，不要加 deprecation shim。

### Phase 2：`RuntimeThread.agent/session` 私有化

改动：

```python
class RuntimeThread:
    def __init__(...):
        self._agent = agent
        self._session = session
```

全文件内部替换：

| 旧 | 新 |
| --- | --- |
| `self.agent` | `self._agent` |
| `self.session` | `self._session` |

runtime 内部需要 raw agent 的地方使用 `_agent`，但不要提供 public property。

如果 `AgentRuntime` 需要取当前 thread 的 agent，使用明确私有 helper：

```python
def _agent_for_runtime(self):
    return self._agent
```

只允许 `src/nanocode/runtime/*` 使用这个 helper。不要让 CLI/RPC/TUI 用它。

验收：

```bash
rg -n "\.agent|\.session" src/nanocode/entrypoints src/nanocode/tui
rg -n "current_thread\.agent|thread\.agent|_thread\.agent" src tests
```

允许出现：

- `src/nanocode/session/agent.py` 内 `self.agent`，因为 `AgentSession` 内部就是包装 agent。
- agent/core 内部字段。
- tests 中明确白盒 agent/session 测试，但 runtime boundary tests 不应依赖 public `.agent`。

### Phase 3：删除旧 compatibility import 面

删除：

- `src/nanocode/agent/runtime.py`
- `src/nanocode/agent/session.py`

修改：

- `src/nanocode/agent/__init__.py`

目标：

```python
__all__ = ["Agent"]

def __getattr__(name: str):
    if name == "Agent":
        from .engine import Agent
        return Agent
    raise AttributeError(name)
```

或者如果还有轻量 agent-only type，可只保留真正属于 agent package 的类型。不要从 `nanocode.agent` re-export runtime/session。

迁移 import：

| 旧 | 新 |
| --- | --- |
| `from nanocode.agent import AgentRuntime` | `from nanocode.runtime import AgentRuntime` |
| `from nanocode.agent import RuntimeThread` | `from nanocode.runtime import RuntimeThread` |
| `from nanocode.agent.runtime import AgentConfig` | `from nanocode.runtime import AgentConfig` |
| `from nanocode.agent import AgentSession` | `from nanocode.session.agent import AgentSession` |
| `from nanocode.agent.session import AgentSession` | `from nanocode.session.agent import AgentSession` |

需要重点改 tests：

- `tests/session/test_child_session.py`
- `tests/session/test_session_lock.py`
- `tests/session/test_p3_review_remediation.py`
- `tests/session/test_p6_tree_command.py`
- `tests/entrypoints/test_sessions_wiring.py`
- `tests/entrypoints/test_repl_dispatch_characterization.py`
- `tests/entrypoints/test_commands_pi.py`
- `tests/entrypoints/test_thread_lifecycle.py`

验收：

```bash
rg -n "nanocode\.agent\.runtime|nanocode\.agent\.session|from nanocode\.agent import .*Runtime|from nanocode\.agent import .*AgentSession" src tests docs
```

预期：源码和测试中无旧 runtime/session import。docs 可保留历史说明，但新权威文档不能推荐旧入口。

### Phase 4：收紧 `Agent.chat()` 与 `_ensure_session_lease()`

当前：

- `Agent.chat()` 是 public turn 入口，见 `src/nanocode/agent/engine.py:370-373`。
- `_ensure_session_lease()` 在缺 runtime 注入时会自取 lease，见 `src/nanocode/agent/engine.py:482-495`。

目标：

1. 生产路径不允许外部直接 `Agent.chat()`。
2. writer lease 只能由 runtime/spawn 注入。
3. 缺 `_session_mgr` 时 fail loud，而不是自取。

建议改法：

```python
async def chat(self, user_message: str) -> None:
    """Internal legacy helper; external callers must use RuntimeThread.run()."""
    await self.agent_session.run_turn(user_message)
```

更激进：

- 改名 `_chat_internal()`。
- tests 全部走 runtime。
- `run_once()` 内部调用 `_chat_internal()`。

`_ensure_session_lease()` 改为：

```python
def _ensure_session_lease(self) -> None:
    if self._session_mgr is None:
        raise RuntimeError(
            "No active session writer lease. Start the agent through AgentRuntime."
        )
```

子 agent 例外：

- 子 agent 也不能自取；必须由 `runtime/spawn.py` 或 test helper 注入 `SessionLease.open_or_create(...).manager`。

测试迁移：

- 现有直接构造 `Agent` 并写 session 的测试，统一用 helper：

```python
def attach_runtime_agent(agent):
    from nanocode.runtime import AgentRuntime
    from nanocode.session.lease import SessionLease
    lease = SessionLease.open_or_create(agent.session_id)
    return AgentRuntime()._attach_agent(agent, lease=lease)
```

或者测试 `AgentSession` 低层行为时，显式说明这是 white-box 并手动注入 lease。

### Phase 5：skill/hook 服务化

当前 `RuntimeThread.invoke_skill()` 是对外 facade，但内部仍直接：

- `get_skill_by_name()`
- `execute_skill()`
- `resolve_skill_prompt()`
- `agent._register_skill_hooks(skill)`

来源：`src/nanocode/runtime/facade.py:814-835`

目标：

- runtime 拥有 `SkillRuntimeService`。
- CLI 只把 unknown slash `/foo` 转交 `RuntimeThread.invoke_skill()`.
- `Agent` 只接收 runtime 已解析的 hook/capability。

建议接口：

```python
class SkillRuntimeService:
    def resolve_user_invocation(self, name: str, args: str) -> SkillInvocation: ...
    def install_hooks(self, agent, skill) -> None: ...
```

短期可以先把 `_register_skill_hooks()` 调用包进 private runtime function，避免 CLI/RPC/TUI 知道它。长期再从 `Agent` 私有名移到稳定 hook service。

### Phase 6：加强 `can_switch()`

当前只检查：

- `self.is_processing`
- `self.agent._background_tasks`

来源：`src/nanocode/runtime/facade.py:698-704`

目标检查：

| 状态 | 原因 |
| --- | --- |
| current turn running | 避免同一 Agent core 被 rebind。 |
| background host tasks running | 避免 task 回写旧 session/service。 |
| live subagent child writer running | 避免 parent session switch 后 child callbacks 写错父上下文。 |
| extension task running with captured ctx | 避免 stale extension context。 |
| pending approval request | 避免 UI response 回到旧 thread。 |
| shell command running | 避免 audit event 写错 session。 |

推荐返回结构：

```python
def can_switch(self) -> tuple[bool, str | None]:
    ...
```

保持当前接口，内部加强即可。

## 6. 落地顺序建议

推荐按以下 PR/commit 切分：

### Commit 1：Boundary tests and approval facade

内容：

- 新增 `RuntimeThread.attach_approvals()`.
- CLI/RPC 改用 facade attach。
- 添加 grep/import boundary tests。

验证：

```bash
PYTHONPATH=src python3 -m pytest \
  tests/entrypoints/test_command_import_boundaries.py \
  tests/entrypoints/test_repl_dispatch_characterization.py \
  tests/entrypoints/test_thread_lifecycle.py
```

### Commit 2：Hide `RuntimeThread.agent/session`

内容：

- `RuntimeThread.agent/session` 改 `_agent/_session`。
- runtime/facade 内部替换。
- entrypoints/TUI/tests 修正。

验证：

```bash
rg -n "current_thread\.agent|thread\.agent|_thread\.agent" src tests
PYTHONPATH=src python3 -m pytest tests/runtime tests/entrypoints
```

### Commit 3：Delete compatibility imports

内容：

- 删除 `src/nanocode/agent/runtime.py`
- 删除 `src/nanocode/agent/session.py`
- 修正 `src/nanocode/agent/__init__.py`
- 修正 tests imports。

验证：

```bash
rg -n "nanocode\.agent\.runtime|nanocode\.agent\.session|from nanocode\.agent import .*Runtime|from nanocode\.agent import .*AgentSession" src tests
PYTHONPATH=src python3 - <<'PY'
import sys
import nanocode.agent
bad = [m for m in sys.modules if m.startswith(("anthropic", "openai", "yaml", "nanocode.runtime"))]
assert not bad, bad
print("ok")
PY
```

### Commit 4：Runtime-owned lease only

内容：

- `_ensure_session_lease()` 改 fail loud。
- 直接 `Agent.chat()` 生产调用迁移到 runtime。
- 测试 helper 显式注入 lease。

验证：

```bash
PYTHONPATH=src python3 -m pytest tests/session tests/subagents tests/runtime
```

### Commit 5：Strengthen switch guard and stale context

内容：

- `can_switch()` 纳入 background subagent、extension task、approval、shell 状态。
- rebind/dispose 后旧 ctx/thread 调用 fail loud。

验证：

```bash
PYTHONPATH=src python3 -m pytest \
  tests/entrypoints/test_thread_lifecycle.py \
  tests/runtime/test_extension_context_lifecycle.py \
  tests/subagents
```

## 7. 风险与处理

### 7.1 白盒测试会大量失败

原因：很多 tests 通过 `from nanocode.agent import AgentRuntime, AgentSession, RuntimeThread` 或直接 `thread.agent` 构造状态。

处理：

- 行为测试改走 `nanocode.runtime`。
- session tree 低层测试可以直接 import `nanocode.session.agent.AgentSession`。
- 真正需要 raw `Agent` 的测试放在 `tests/agent/`，并显式注入 lease。

不要做：

- 不要为了测试保留 public `.agent`。
- 不要新增 `RuntimeThread.agent_for_tests`。

### 7.2 embedded 用户可能依赖旧 import

处理：

- 激进 cutover：直接删，不做兼容。
- 新文档只推荐：

```python
from nanocode.runtime import AgentRuntime, AgentConfig
```

不要做：

- 不要在 `nanocode.agent.__getattr__` 里继续 lazy return runtime symbols。
- 不要保留 `nanocode.agent.runtime` re-export。

### 7.3 runtime/facade.py 内部仍会读很多 Agent 私有字段

短期可接受，因为这是 runtime 内部。重点是不要让外部拿 raw agent。

后续可逐步抽：

- `TaskRuntimeService`
- `SkillRuntimeService`
- `SubagentRuntimeService`
- `MemoryRuntimeService`
- `ApprovalRuntimeService`

但第一轮不要过度抽象。先把外部边界切干净。

### 7.4 Pi 自己公开 `AgentSession.agent`，为什么 nanocode 不学？

Pi 的源码确实这样：

- `packages/coding-agent/src/core/agent-session.ts:265-267`

但 Pi 同时把 replacement、services、dispose 放 runtime：

- `packages/coding-agent/src/core/agent-session-runtime.ts:67-74`
- `packages/coding-agent/src/core/agent-session-runtime.ts:167-181`
- `packages/coding-agent/src/core/agent-session.ts:728-744`

nanocode 的目标是“内部像 Pi，外部像 Codex”，而不是照抄 Pi 所有 public 宽松点。Codex 的 public handle 边界更适合嵌入式 API：

- `codex-rs/core/src/lib.rs:17`
- `codex-rs/core/src/codex_thread.rs:160-166`
- `codex-rs/core/src/codex_thread.rs:194-196`
- `codex-rs/core/src/codex_thread.rs:414-420`

## 8. 最终验收清单

### 8.1 静态验收

```bash
# 外部层不得访问 raw agent/session
rg -n "current_thread\.agent|thread\.agent|_thread\.agent|\.agent_session|_session_mgr|_background_tasks|task_manager" \
  src/nanocode/entrypoints src/nanocode/tui

# 不得保留旧 runtime/session import 面
rg -n "nanocode\.agent\.runtime|nanocode\.agent\.session|from nanocode\.agent import .*Runtime|from nanocode\.agent import .*AgentSession" \
  src tests

# RuntimeThread 不得公开 agent/session
PYTHONPATH=src python3 - <<'PY'
from nanocode.runtime import RuntimeThread
assert not hasattr(RuntimeThread, "agent")
assert not hasattr(RuntimeThread, "session")
print("ok")
PY
```

第一条 grep 预期无输出。第三条只检查 class attribute 不够，最终应配合实例测试。

### 8.2 import boundary 验收

```bash
PYTHONPATH=src python3 - <<'PY'
import sys
import nanocode.agent
bad = [
    name for name in sys.modules
    if name.startswith(("anthropic", "openai", "yaml", "nanocode.runtime"))
]
assert not bad, bad
print("nanocode.agent import boundary ok")
PY
```

### 8.3 lifecycle 验收

覆盖场景：

- startup creates canonical session with lease。
- `--resume` busy session fail-closed。
- `/new` invalidates old thread。
- `/resume` rebinds services to target cwd。
- `/fork` only accepts user message entry。
- `/clone` copies current branch。
- corrupt session does not release old session first。
- rebind failure does not leak new lease。
- old thread cannot run after replacement。

### 8.4 RPC/TUI 验收

覆盖场景：

- RPC prompt 只能串行。
- RPC approval request/response 通过 request id 或 broker 队列回到 current thread。
- rebind 后 RPC subscription 切到新 thread。
- TUI rebind 后 transcript/status/footer 来自新 thread。
- 事件流全是 JSON-able envelope。

### 8.5 shell/skill/task/subagent 验收

覆盖场景：

- `!shell` 发 `user_shell_started` / `user_shell_completed` runtime events。
- shell timeout/error/audit 均不进模型工具权限。
- `/skill` 不由 CLI 直接调用 skill registry。
- task list/output/stop 只经 runtime facade。
- subagent widget/conversation/cancel 只经 runtime snapshot，不读 sidecar/session 文件。

## 9. 推荐完成后的 public API 示例

```python
from nanocode.runtime import AgentConfig, AgentRuntime

runtime = AgentRuntime()
thread = runtime.thread_start(AgentConfig(cwd="/path/to/project"))

thread.attach_approvals(confirm_fn=my_confirm, plan_approval_fn=my_plan_approval)
unsubscribe = thread.subscribe(lambda event: print(event))

result = await thread.run("inspect this repository")
print(result.final_response)

state = thread.state()
messages = thread.messages()
thread.cancel()
thread.release_lease()
unsubscribe()
```

不再支持：

```python
from nanocode.agent import AgentRuntime
from nanocode.agent.runtime import AgentConfig
thread.agent.chat("...")
thread.agent._session_mgr.append_message(...)
```

## 10. 一句话准则

`Agent` 可以存在，但只能被 runtime/session 内部使用；任何跨 CLI/RPC/TUI/SDK 边界的动作，都必须表现为 `RuntimeThread` 或 runtime service 的显式操作，并落到 canonical `session.jsonl`、JSON-able event envelope、`SessionLease` 单写者这三个不变量上。

