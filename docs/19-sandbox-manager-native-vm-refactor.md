# 19 · SandboxManager native-first / VM-on-demand refactor

状态：实施规格草案（2026-06-15）

目标：把当前分散在 `run_shell` / `sandbox_shell` / `execute` / `permissions` / `/sandbox` 的沙箱逻辑，收敛成可嵌入 runtime 友好的 `SandboxManager`。默认使用 Codex 风格 OS sandbox；只有更严格 profile、显式 VM 要求或策略无法被 native 后端表达时，才启动 microsandbox VM。本文档必须在 0 上下文情况下可落地，不依赖任何对话历史。

## 1. 一句话结论

采用三档执行后端：

| 后端 | 名称 | 默认用途 | 是否默认 |
|---|---|---|---|
| native | seatbelt / bwrap / 后续 landlock | 普通 `run_shell`、测试、构建、git 只读/工作区写入 | 是 |
| vm | microsandbox | 不可信代码、更严格隔离、依赖污染隔离、显式 VM profile | 否 |
| host | 无沙箱宿主执行 | 仅审批通过的显式 escalation | 否 |

关键原则：

```text
默认 native OS sandbox。
严格时升级到 VM。
永不从 sandbox 自动降级到 host。
模型不能控制 cwd / session_id / mount / network / engine。
嵌入式 API 只暴露 policy，不暴露 adapter 细节。
```

## 2. 为什么要改

当前实现的实际问题不是 microsandbox 某几个参数，而是层次错位：

- `sandbox_shell` 是模型可见工具，模型能直接传 `network`、`mount_workspace`、`deps`、`persist`。
- `sandbox_shell._merge_params()` 在执行时合并 `/sandbox` 模块级默认值，权限层看到的是未合并 raw input。
- `_cwd`、`_session_id` 是隐藏字段，但当前可以从 raw input 进入执行路径。
- `execute._route_run_shell()`、`run_shell.plan_shell()`、`permissions.check_permission()`、`sandbox_shell.run()` 各自掌握一部分策略。
- microsandbox 不可用时存在“提示 use run_shell”这类降级语义，不符合 fail-closed。

这些问题会破坏三个边界：

1. 安全边界：模型可以影响 host path mount、network、VM lifecycle。
2. 嵌入式边界：SDK/AppServer/CLI 未来会被迫理解 `msb`、volume、`_cwd` 等低层细节。
3. 设计边界：PermissionEngine 不是唯一 gate，tool executor 仍在做 policy merge。

## 3. 参考源码

### 3.1 nanocode 当前源码

实施前必须阅读这些文件：

| 文件 | 需要关注的点 |
|---|---|
| `src/nanocode/tools/sandbox_shell.py` | 当前 microsandbox schema、`_merge_params()`、`_common_resource_flags()`、persist fingerprint、`msb list` substring |
| `src/nanocode/tools/run_shell.py` | `plan_shell()` 当前 host/native/microvm 路由 |
| `src/nanocode/tools/execute.py` | `_route_run_shell()` 当前前台 shell 分发和 microVM 输入重组 |
| `src/nanocode/tools/permissions.py` | `check_permission()` 当前 raw dict 权限判断、protected path、escalate、sandbox_shell confirm |
| `src/nanocode/capabilities/router.py` | 当前 dispatch chokepoint、`_session_id` 注入、hook 包裹 |
| `src/nanocode/capabilities/permissions.py` | `PermissionContext` 的不可变上下文雏形 |
| `src/nanocode/tools/spec.py` | ToolSpec 单一工具表，后续要移除 public `sandbox_shell` |
| `src/nanocode/tools/sandbox_backends/{seatbelt,bwrap}.py` | native sandbox adapter 的现有基础 |
| `src/nanocode/entrypoints/commands/builtin.py` | `/sandbox` 模块默认值命令，需要删除或替换 |
| `docs/12-embeddable-agent-layered-refactor.md` | L0-L7 分层：sandbox 属 L1 capabilities + L0 adapters，不属于 public client |
| `docs/16-pi-alignment-aggressive-convergence.md` | 不保留老旧兼容、PermissionEngine/allowlist fail-closed 不变量 |

已知 bug 必须覆盖：

1. `_cwd` spoof：`sandbox_shell._common_resource_flags()` 使用 `Path(p.get("_cwd") or Path.cwd())` 决定 workspace mount。
2. `/sandbox` defaults 绕过权限：权限层检查 raw input，执行层才 merge defaults。
3. persist fingerprint 缺 create-time immutable 字段：workspace realpath、trace mount/env/tag、protected roots version 等。
4. `msb list` 用 substring 判断 sandbox 是否存在。

### 3.2 Pi 源码参考

本地参考路径：`/private/tmp/pi-src` 或 `/tmp/pi-src`。

重点文件：

| 文件 | 设计点 |
|---|---|
| `packages/agent/src/agent-loop.ts` | `prepareToolCallArguments()` -> `validateToolArguments()` -> `beforeToolCall()` -> execute；工具执行只拿 validated args |
| `packages/coding-agent/src/core/tools/bash.ts` | bash tool 的 `cwd` 在创建 tool definition 时由 runtime 绑定，不来自模型参数 |
| `packages/agent/src/harness/session/session.ts` | session context 由 append-only tree 投影，state 是投影不是策略来源 |
| `packages/agent/src/harness/session/jsonl-storage.ts` | append-only storage 参考，不是沙箱重点 |

要抄的设计：

- 工具调用先 prepare/validate，再 pre-tool gate，再执行。
- 执行拿 validated args，不拿模型 raw dict。
- `cwd` 是 runtime context，不是 tool input。

不要抄的部分：

- Pi 的 extension permission 示例只是弱 gate，不足以替代 nanocode 的 fail-closed PermissionEngine。

### 3.3 OpenAI Codex 源码和文档参考

官方文档参考：

- Codex manual: Agent approvals & security / Sandbox / Permissions / Hooks / Subagents。
- 本地缓存曾位于 `/var/folders/7z/j5sd4zzj0bb3ldsf5gqndvwc0000gn/T/openai-docs-cache/codex-manual.md`。

OpenAI Codex upstream 源码参考：

| upstream 文件 | 设计点 |
|---|---|
| `codex-rs/core/src/exec.rs` | `ExecParams` 强类型执行参数，`cwd`、network、sandbox permissions 分离 |
| `codex-rs/core/src/exec_policy.rs` | approval policy -> `ExecApprovalRequirement`，prompt 不可用时 forbidden |
| `codex-rs/core/src/tools/sandboxing.rs` | `ExecApprovalRequirement`、`SandboxOverride`、`ToolRuntime`、`SandboxAttempt` |
| `codex-rs/core/src/tools/runtimes/unified_exec.rs` | `UnifiedExecRequest` 同时持有 `cwd` 和 `sandbox_cwd`，approval key 包含 cwd/permissions |
| `codex-rs/core/src/sandboxing/mod.rs` | core-owned `ExecRequest` adapter，policy transform 与 exec metadata 分离 |
| `codex-rs/core/src/config/resolved_permission_profile.rs` | permission profile snapshot：resolved profile + active id + workspace roots 原子绑定 |

对应 upstream URL：

```text
https://github.com/openai/codex/blob/main/codex-rs/core/src/exec.rs
https://github.com/openai/codex/blob/main/codex-rs/core/src/exec_policy.rs
https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/sandboxing.rs
https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/runtimes/unified_exec.rs
https://github.com/openai/codex/blob/main/codex-rs/core/src/sandboxing/mod.rs
https://github.com/openai/codex/blob/main/codex-rs/core/src/config/resolved_permission_profile.rs
```

要抄的设计：

- sandbox 是技术边界，approval 是越界许可。
- 默认 workspace-write + network off + on-request approvals。
- protected roots 在 writable workspace 内仍只读。
- network 默认 off；启用时应 allowlist-first，local/private 默认拒绝。
- subagent 继承 parent sandbox/approval policy；非交互无法新审批时 fail closed。
- hooks 使用同一审批和 sandbox 边界；project-local hook 要 trust。

## 4. 不变量

这些是不允许被实现细节破坏的硬边界。

### 4.1 嵌入式边界

```text
Client / SDK / AppServer
    只调用 Runtime API

Runtime Facade
    持有 session/thread/approval/context

Capabilities
    PermissionEngine + SandboxManager 决策

Platform Adapters
    seatbelt / bwrap / microsandbox / host exec
```

规则：

1. public Runtime API 可以暴露 `permission_profile` / `sandbox_policy`，不能暴露 `msb` volume、`mount_workspace`、`_cwd`、`_session_id`。
2. 模型 schema 不能暴露 adapter 选择权。模型请求 shell，runtime 决定 native/vm/host/deny。
3. `cwd`、`session_id`、workspace roots、subagent 身份、interactive 状态只能由 `HostContext` 提供。
4. `SandboxManager` 是唯一规划点；adapter 只执行计划。
5. SDK/AppServer/CLI 都使用同一 Runtime/Capability 面，不 import `sandbox_shell` 实现细节。

### 4.2 安全边界

1. allowlist + PermissionEngine 必须在真实工具执行前 fail-closed。
2. unknown input key、leading underscore key 必须 reject，不 silent strip。
3. `escalate` / host execution 永远需要明确审批，且不能越过 protected roots / denied reads。
4. native sandbox 不可用时，不自动 host fallback。
5. VM 不可用且策略要求 VM 时，deny。
6. 后端机制失败不等于授权扩大。

### 4.3 默认策略

默认 profile：

```text
filesystem = workspace-write
network = none
approval = on-request
engine = auto
protected_roots = .git, .nanocode, .codex, .agents, .claude
```

`engine=auto` 的语义：

```text
first choice: native OS sandbox
upgrade to VM: only when policy requires stronger isolation or native cannot express policy and VM can
deny: no backend can enforce policy
never: downgrade to host
```

## 5. 目标数据结构

新建 `src/nanocode/capabilities/sandbox.py`。第一版先写 dataclass + pure planner，不接执行。

建议类型：

```python
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

class SandboxEngine(str, Enum):
    AUTO = "auto"
    NATIVE = "native"
    VM = "vm"
    HOST = "host"

class SandboxBackend(str, Enum):
    NATIVE = "native"
    MICROVM = "microvm"
    HOST = "host"

class NetworkMode(str, Enum):
    NONE = "none"
    ALLOWLIST = "allowlist"
    FULL = "full"

@dataclass(frozen=True)
class HostContext:
    cwd: Path
    session_id: str
    workspace_roots: tuple[Path, ...]
    temp_roots: tuple[Path, ...]
    interactive: bool
    is_subagent: bool
    is_background: bool
    is_hook: bool
    approval_mode: str

@dataclass(frozen=True)
class ShellRequest:
    command: str
    timeout_ms: int
    run_in_background: bool = False
    escalate: bool = False

@dataclass(frozen=True)
class FileSystemPolicy:
    readable_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...]
    denied_roots: tuple[Path, ...]
    protected_roots: tuple[Path, ...]

@dataclass(frozen=True)
class NetworkPolicy:
    mode: NetworkMode
    allow_domains: tuple[str, ...] = ()
    deny_domains: tuple[str, ...] = ()
    allow_local: bool = False

@dataclass(frozen=True)
class SandboxPolicy:
    engine: SandboxEngine
    filesystem: FileSystemPolicy
    network: NetworkPolicy
    approval_mode: str
    vm_image: str = "python:3.12"
    vm_persist: bool = False

@dataclass(frozen=True)
class SandboxPlan:
    backend: SandboxBackend
    command: str
    cwd: Path
    timeout_ms: int
    filesystem: FileSystemPolicy
    network: NetworkPolicy
    session_id: str
    env: tuple[tuple[str, str], ...] = ()
    vm_image: str | None = None
    vm_name: str | None = None
    vm_fingerprint: str | None = None
```

`SandboxManager` 第一版 API：

```python
class SandboxManager:
    def plan_shell(
        self,
        request: ShellRequest,
        host: HostContext,
        policy: SandboxPolicy,
        approval: "ApprovalDecision",
    ) -> "SandboxPlan | SandboxDeny":
        ...

    async def execute_shell(
        self,
        request: ShellRequest,
        host: HostContext,
        policy: SandboxPolicy,
    ) -> str:
        ...
```

注意：

- `ShellRequest` 是 public args 解析结果。
- `HostContext` 是 runtime 注入。
- `SandboxPolicy` 是 session/profile 状态投影。
- `SandboxPlan` 是 adapter 唯一输入。
- adapter 不再读 raw dict。

## 6. Tool schema 目标

### 6.1 `run_shell`

保留一个 public shell 工具：

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "command": {"type": "string"},
    "timeout": {"type": "number"},
    "run_in_background": {"type": "boolean"},
    "escalate": {"type": "boolean"}
  },
  "required": ["command"]
}
```

`escalate` 是否继续暴露给模型由实现时决定。若保留，必须有两条限制：

1. 只有 sandbox 失败提示明确要求 retry escalation 时才允许。
2. PermissionEngine 看到 `escalate=True` 必须 confirm；非交互直接 deny。

### 6.2 `sandbox_shell`

目标：不再作为默认模型工具暴露。

可选处理：

1. 直接从 `TOOLS` 移除 `sandbox_shell`。
2. 保留为 internal/debug-only 工具，不进入 API tool list。
3. 迁移测试只测 `MicrosandboxAdapter`，不测 public tool schema。

不允许保留为模型可直接调用的 VM 权限控制面。

## 7. Policy 和 engine matrix

### 7.1 Profiles

| profile | engine | filesystem | network | approval | 说明 |
|---|---|---|---|---|---|
| `default` | auto | workspace-write | none | on-request | 默认 Codex 风格 |
| `read-only` | native | read-only | none | on-request | 计划/审查 |
| `strict` | auto | workspace-write + tighter deny reads | none/allowlist | on-request | 可升级 VM |
| `vm` | vm | policy-derived mount | none | on-request | 强制 microVM |
| `danger-full-access` | host | unrestricted | full | explicit | 只能通过明确配置/审批 |

### 7.2 Engine selection

伪代码：

```python
def choose_backend(policy, request, native_available, vm_available):
    if policy.engine == HOST:
        return HOST if request.escalate and approval.approved else DENY

    if request.escalate:
        return HOST only if approval.approved and unsandboxed_execution_allowed(policy)

    if policy.engine == VM:
        return MICROVM if vm_available else DENY

    if policy.engine == NATIVE:
        return NATIVE if native_available and native_can_enforce(policy) else DENY

    # AUTO
    if native_available and native_can_enforce(policy) and not requires_vm(policy, request):
        return NATIVE
    if vm_available and vm_can_enforce(policy):
        return MICROVM
    return DENY
```

`requires_vm(policy, request)` 第一版只允许保守触发：

- profile 明确要求 stronger isolation。
- hook/subagent/background policy 标记 `vm_required`。
- 用户/系统配置明确 `engine=vm`。
- 后续可以增加不可信代码启发式，但第一版不要做复杂命令语义猜测。

## 8. 分阶段落地计划

每一步都可以独立 PR。用户已定调“不管老旧测试和兼容，不要兜底”，所以旧测试可以删除/重写，不做 flag-gated 双路径。

### Phase 0 · Baseline 和测试冻结

目标：记录当前 bug，先写失败测试或 characterization，避免改造中忘掉安全洞。

涉及文件：

- `tests/capabilities/test_sandbox_planning.py` 新建
- `tests/tools/test_sandbox_shell.py` 可拆迁/删除旧语义
- `tests/tools/test_permissions.py`

新增测试：

1. `run_shell` 输入包含 `_cwd` -> validation error。
2. `sandbox_shell` 不在默认 `TOOLS`。
3. default policy 下 `run_shell` plan backend 是 native。
4. native unavailable + engine auto + vm unavailable -> deny。
5. native unavailable + engine auto + vm available + policy allows vm -> microVM。
6. `escalate=True` + noninteractive -> deny。
7. protected root 写入不因 host escalation 自动放开。

验收：

```bash
PYTHONPATH=src python3 -m pytest -q tests/capabilities/test_sandbox_planning.py
```

### Phase 1 · 严格工具输入校验

目标：模型 raw input 在进入 permission 前变成 validated public args。

涉及文件：

- `src/nanocode/capabilities/router.py`
- `src/nanocode/tools/spec.py`
- `src/nanocode/tools/run_shell.py`
- 各工具 schema

改动：

1. 所有 public tool schema 加 `additionalProperties: false`。
2. 在 `CapabilityRouter.dispatch()` 真实工具执行前调用统一 validator。
3. validator 规则：
   - unknown key reject。
   - key 以 `_` 开头 reject。
   - required 缺失 reject。
   - 类型不符 reject。
4. 删除 router 中对 `inp["_session_id"]` 的注入。
5. `run_shell.run_structured()` 不再读 `inp.get("_cwd")`。

验收测试：

- `{"command": "pwd", "_cwd": "/"}` -> error，不进 permission，不进 executor。
- `{"command": "pwd", "mount_workspace": true}` -> error。
- 正常 `{"command": "pwd"}` -> 继续进入 planning。

### Phase 2 · HostContext 和 PermissionContext 扩展

目标：把 runtime 信息从 dict 字段迁入 typed context。

涉及文件：

- `src/nanocode/capabilities/permissions.py`
- `src/nanocode/capabilities/host.py`
- `src/nanocode/runtime/facade.py`
- `src/nanocode/runtime/spawn.py`
- `src/nanocode/agent/engine.py` 中当前 host protocol wrapper

改动：

1. `ToolHost` 增加获取 host context 的稳定方法：

```python
def host_context(self, *, background: bool = False, hook: bool = False) -> HostContext: ...
def permission_context(self) -> PermissionContext: ...
def sandbox_policy(self) -> SandboxPolicy: ...
```

2. `PermissionContext` 增加：
   - `interactive`
   - `is_subagent`
   - `is_background`
   - `is_hook`
   - `workspace_roots`
   - `approval_mode`

3. `PermissionEngine` 不再读 `_cwd` / `_session_id` / module defaults。

验收：

- subagent/background/hook 能构造不同 context。
- 非交互 context 下 `request_approval` 被转成 deny。

### Phase 3 · 新建 SandboxManager pure planner

目标：先不执行，只把策略规划做成纯函数。

涉及文件：

- `src/nanocode/capabilities/sandbox.py` 新建
- `tests/capabilities/test_sandbox_planning.py`

改动：

1. 定义第 5 节 dataclass。
2. 实现：
   - `default_sandbox_policy(host)`
   - `protected_roots_for_workspace(root)`
   - `.git` pointer real gitdir 解析
   - `native_can_enforce(policy)`
   - `vm_can_enforce(policy)`
   - `choose_backend(...)`
   - `plan_shell(...)`
3. plan 返回结构化 deny，而不是字符串。

验收：

- default -> native。
- strict vm-required -> microVM。
- engine native + native missing -> deny。
- engine vm + msb missing -> deny。
- auto 不 host fallback。
- protected roots 包含 `.git` pointer target。

### Phase 4 · native sandbox 迁入 SandboxManager

目标：`run_shell` 默认路径由 manager 调 native adapter。

涉及文件：

- `src/nanocode/tools/execute.py`
- `src/nanocode/tools/run_shell.py`
- `src/nanocode/tools/sandbox_backends/base.py`
- `src/nanocode/tools/sandbox_backends/seatbelt.py`
- `src/nanocode/tools/sandbox_backends/bwrap.py`
- `src/nanocode/capabilities/sandbox.py`

改动：

1. 删除或停止使用 `run_shell.plan_shell()`。
2. 删除或停止使用 `execute._route_run_shell()`。
3. `execute_tool("run_shell")` 调 `SandboxManager.execute_shell()`。
4. native adapter 接收 `SandboxPlan`，不接 raw dict。
5. native adapter cwd 使用 `plan.cwd`。
6. native adapter protected roots 从 `plan.filesystem.protected_roots` 生成 profile。

验收：

- `run_shell pwd` 在 runtime cwd 下执行。
- 模型无法改变 cwd。
- native unavailable 不 host fallback。
- shell 失败和 sandbox mechanism failure 文案区分，但都不自动扩大权限。

### Phase 5 · MicrosandboxAdapter 重写

目标：把 `sandbox_shell.py` 从 public tool 改成 VM adapter。

建议新文件：

- `src/nanocode/tools/sandbox_backends/microsandbox.py`

可选保留文件：

- `src/nanocode/tools/sandbox_shell.py` 只保留 compatibility import 会违反“不兼容”定调，建议删除或改为 debug-only 且不进 `TOOLS`。

改动：

1. 删除 `_merge_params()`。
2. 删除 `sandbox_defaults` 依赖。
3. 删除 `_session_id_of(p)` 从 dict 读隐藏字段。
4. 删除 `_common_resource_flags(p)` raw dict 版本，改为 `flags_from_plan(plan)`。
5. VM workspace mount 来自 `plan.filesystem.writable_roots` 和 `plan.cwd`，不是模型 bool。
6. VM network 来自 `plan.network`。
7. deps/install 第一版不要做模型参数；如需要，用 policy 字段显式表达。
8. persist fingerprint 包含：
   - adapter version
   - image
   - cpus/memory
   - network policy canonical JSON
   - workspace mount realpaths
   - filesystem protected/denied roots canonical JSON
   - deps volume mode
   - trace mount/env/tag
   - session id / vm name
9. `msb list` 精确解析。不要 substring。
10. `msb` missing -> structured deny/error，不提示 use run_shell。

验收：

- `_cwd=/` 无入口。
- `/workspace` mount realpath 等于 `HostContext.cwd` 或 policy roots。
- fingerprint 对 workspace realpath / trace / network / protected roots 变化敏感。
- `abc` 不匹配 `abc2`。

### Phase 6 · 删除 `/sandbox` defaults 和 public `sandbox_shell`

目标：消灭执行时 mutable module defaults。

涉及文件：

- `src/nanocode/tools/sandbox_defaults.py`
- `src/nanocode/entrypoints/commands/builtin.py`
- `src/nanocode/tools/spec.py`
- `src/nanocode/entrypoints/render.py`
- `src/nanocode/tui/tooltext.py`
- 测试中所有 `sandbox_shell` public tool 断言

改动：

1. 删除 `sandbox_defaults.py`。
2. 删除 `/sandbox` 命令，或改成 `/permissions` profile 展示/切换。
3. 从 `TOOLS` 默认列表移除 `sandbox_shell`。
4. UI render 中 `sandbox_shell` 展示逻辑删除或改为 internal backend tag。
5. prompt/tool list 不再向模型描述 VM 参数。

验收：

- 默认工具列表不包含 `sandbox_shell`。
- `/sandbox network public` 不存在。
- profile 变化必须写入 runtime/session state，而不是 module global。

### Phase 7 · Runtime / embeddable API 收口

目标：让 CLI、未来 SDK、未来 AppServer 都只操作 Runtime policy。

涉及文件：

- `src/nanocode/runtime/facade.py`
- `src/nanocode/runtime/__init__.py`
- `src/nanocode/agent/models.py` 或 config dataclass 所在文件
- `src/nanocode/entrypoints/cli.py`

新增 public config：

```python
AgentConfig(
    permission_mode="default",
    sandbox_profile="default",
    # or
    sandbox_policy=SandboxPolicy(...)
)
```

规则：

1. public config 可以选 profile。
2. public config 不可以传 adapter argv。
3. AppServer/SDK 后续只序列化 policy，不序列化 `msb` implementation fields。

验收：

- in-process runtime 能启动 default/native。
- profile 切换能影响 planning。
- 不需要 CLI-specific global env 才能启用 sandbox。

### Phase 8 · hooks / subagents / background 统一

目标：所有 shell 入口使用同一 manager。

涉及文件：

- `src/nanocode/runtime/spawn.py`
- `src/nanocode/tasks/runner.py`
- `src/nanocode/capabilities/router.py`
- hook 执行相关位置

改动：

1. background shell 不直接调用 `run_shell.run_background(inp)` raw dict。
2. background 构造 `HostContext(is_background=True)`。
3. hook shell 构造 `HostContext(is_hook=True)`。
4. subagent policy = parent effective policy 收窄，不能放宽。
5. 需要新审批但 context 不可审批时 deny。

验收：

- read-only subagent 不能通过 hook 获得 shell。
- background 需要 host escalation -> deny。
- hook 无 native backend 且 policy 不允许 VM -> deny。
- subagent `engine=vm` 能触发 VM adapter，但不能打开 parent 未允许的 network。

### Phase 9 · 测试和旧测试清理

目标：测试按新行为重写，不保留旧兼容。

建议新增测试目录：

```text
tests/capabilities/test_sandbox_planning.py
tests/capabilities/test_tool_validation.py
tests/capabilities/test_shell_execution_policy.py
tests/tools/test_microsandbox_adapter.py
tests/runtime/test_sandbox_policy_context.py
tests/subagents/test_sandbox_inheritance.py
```

必须覆盖：

1. hidden keys rejected。
2. unknown keys rejected。
3. default native。
4. VM only strict。
5. native unavailable deny or VM upgrade only when policy allows。
6. no host fallback。
7. network default none。
8. protected roots readonly。
9. `.git` pointer target protected。
10. noninteractive approval request -> deny。
11. subagent inherits and narrows。
12. hook runs through same manager。
13. microsandbox fingerprint exact。
14. `msb list` exact parse。

推荐验证命令：

```bash
PYTHONPATH=src python3 -m pytest -q \
  tests/capabilities/test_sandbox_planning.py \
  tests/capabilities/test_tool_validation.py \
  tests/capabilities/test_shell_execution_policy.py \
  tests/tools/test_microsandbox_adapter.py \
  tests/runtime/test_sandbox_policy_context.py \
  tests/subagents/test_sandbox_inheritance.py
```

最后再跑：

```bash
PYTHONPATH=src python3 -m pytest -q
```

旧测试如果断言这些行为，可以删除或重写：

- `sandbox_shell` 是 public tool。
- `/sandbox` module defaults。
- native unavailable 时提示 host retry。
- read-only command 走 host。
- `_session_id` 从 tool input 注入。
- `_cwd` 从 tool input 影响 cwd。

## 9. 删除清单

这些删除项应和替换实现同 PR 落地，不留后续清理。

| 删除项 | 替代 |
|---|---|
| `tools/sandbox_defaults.py` | runtime/session `SandboxPolicy` |
| `/sandbox <key> <value>` module global defaults | `/permissions` profile 切换或 runtime API |
| public `sandbox_shell` in `TOOLS` | internal `MicrosandboxAdapter` |
| `sandbox_shell._merge_params()` | `SandboxManager.plan_shell()` |
| `sandbox_shell` raw `_cwd` / `_session_id` | `HostContext` |
| `run_shell.plan_shell()` | `SandboxManager` |
| `execute._route_run_shell()` | `SandboxManager.execute_shell()` |
| permission raw checks for `sandbox_shell` model params | permission over typed `ToolAction` / `SandboxPolicy` |
| `msb list` substring exists check | exact parser |
| host fallback wording | structured deny / approval request |

## 10. Implementation notes

### 10.1 Validation placement

Validation 必须在 `CapabilityRouter.dispatch()` 中真实工具分发前完成。不要放在每个 tool 的 `run()` 内，因为：

- permission check 必须看到 validated args。
- hooks/subagents/background 也必须共用。
- Pi 的参考就是 validate before `beforeToolCall`。

### 10.2 Permission placement

PermissionEngine 不应该自己猜 adapter。它只回答：

```text
allow
deny
request_approval(reason)
```

`request_approval` 在非交互上下文直接变 deny。

SandboxManager 再根据 approval result 规划 backend。

### 10.3 Network

第一版不要实现复杂网络代理，只要保证：

- 默认 network none。
- `network != none` 必须来自 policy，不来自模型。
- native 和 VM adapter 都必须能收到 `NetworkPolicy`。
- 如果 backend 不能 enforce requested network policy，deny。

### 10.4 Protected roots

protected roots 是 policy 的一部分，不是 permission.py 里的 ad hoc write_file 特例。

第一版默认：

```text
.git
.nanocode
.codex
.agents
.claude
```

`.git` 若是文件且内容为 `gitdir: ...`，解析 target realpath 并加入 protected roots。

### 10.5 Microsandbox persist

VM persist 是 adapter lifecycle，不是用户默认参数。

第一版可以直接禁用 persist，先做 ephemeral VM。若要保留 persist，必须：

- fingerprint 完整。
- mismatch 不 silent reuse。
- rebuild 是否 destructive 由 policy/approval 决定。
- VM name 来源于 runtime session id，不来自模型。

## 11. 预期最终文件结构

建议最终结构：

```text
src/nanocode/capabilities/
  sandbox.py              # HostContext/SandboxPolicy/SandboxManager/planner
  permissions.py          # PermissionContext + typed decision
  router.py               # validation + dispatch chokepoint

src/nanocode/tools/
  run_shell.py            # public schema + formatting only
  execute.py              # generic dispatch, no shell routing policy
  sandbox_backends/
    base.py
    seatbelt.py
    bwrap.py
    microsandbox.py       # VM adapter, no public model schema
```

不应存在：

```text
src/nanocode/tools/sandbox_defaults.py
public sandbox_shell in ToolSpec
tool input _cwd / _session_id
```

## 12. 验收标准

功能验收：

- 默认 `run_shell` 使用 native OS sandbox。
- 严格/VM profile 使用 microsandbox。
- native 不可用时不 host fallback。
- VM 不可用且 required 时 fail closed。
- 普通开发命令不默认启动 VM。
- runtime cwd 正确。

安全验收：

- 模型不能 spoof `_cwd`。
- 模型不能打开 network/mount/deps。
- `/sandbox` defaults 不能绕过 permission。
- protected roots 在 workspace-write 中仍只读。
- subagent 不能放宽 parent policy。
- hook/background 不可审批时 deny。

嵌入式验收：

- CLI/SDK/AppServer 只需要传 Runtime config/profile。
- public API 不出现 `msb`、volume、mount、adapter argv。
- SandboxManager 可用 fake backend 单测。
- Platform adapter 可替换。

代码质量验收：

- shell policy 只有一个 planner。
- permission check 只吃 typed action/context。
- adapter 只执行 plan。
- 无 silent fallback。
- 删除旧 public `sandbox_shell` 和 `sandbox_defaults`。

## 13. 推荐 PR 顺序

1. `Add sandbox policy dataclasses and planner tests`
2. `Reject unknown and hidden tool input keys`
3. `Route run_shell through SandboxManager native backend`
4. `Convert microsandbox to VM adapter`
5. `Remove sandbox_shell public tool and sandbox defaults`
6. `Move sandbox profile state into runtime config`
7. `Unify background hooks and subagents under SandboxManager`
8. `Rewrite sandbox tests for native-first VM-on-demand policy`

每个 PR 都应包含：

- 代码改动。
- 对应测试。
- 删除旧测试或说明为何旧语义作废。
- `PYTHONPATH=src python3 -m pytest -q <targeted tests>` 输出。

## 14. 失败模式和处理

| 失败 | 正确处理 |
|---|---|
| native backend missing | engine auto 可尝试 VM；否则 deny |
| VM backend missing | policy required VM 时 deny |
| network requested but backend cannot enforce | deny |
| approval needed but noninteractive | deny |
| protected roots conflict with write request | deny or approval only if policy allows; default deny |
| raw input has hidden key | reject before permission |
| adapter crashes | return mechanism error，不扩大权限 |
| persist fingerprint mismatch | reject or explicitly rebuild；不 silent reuse |

## 15. 给实施者的最短路径

如果只能按最小闭环落地，按这个顺序做：

1. 加 validator，封死 `_cwd` / unknown keys。
2. 加 `SandboxManager.plan_shell()` pure tests。
3. 让 `run_shell` 默认走 native plan。
4. 从 tool list 移除 `sandbox_shell`。
5. 把 microsandbox 改成 adapter。
6. 删除 `/sandbox` defaults。

做到这六步，即使后续 Runtime/AppServer 还没完全平台化，也已经修复当前 microsandbox 的核心边界问题，并且不会破坏嵌入式方向。
