"""capabilities/sandbox.py — SandboxManager：native-first / VM-on-demand 执行规划与执行（docs/19）。

shell 执行的**唯一规划点**。模型只请求 shell；runtime 经 `HostContext` 注入 cwd / session /
workspace / 身份 / interactive，`SandboxPolicy` 投影 session/profile 策略，`SandboxManager`
据此选后端（native / vm / host / deny）并产出 `SandboxPlan`——adapter 的唯一输入。

边界（docs/19 §4，不允许被实现细节破坏）：

- public API 只暴露 policy / profile，不暴露 adapter argv、`msb`、mount、`_cwd`、`_session_id`。
- 模型 schema 不暴露 adapter 选择权：模型请求 shell，runtime 决定 native/vm/host/deny。
- cwd / session_id / workspace roots / subagent 身份 / interactive **只来自 HostContext**。
- 默认 native OS sandbox；严格/显式 VM 才升级；**永不从 sandbox 自动降级到 host**。
- native 不可用不自动 host fallback；VM 不可用且策略要求 VM → deny。
- 后端机制失败 != 授权扩大。

本模块刻意是 **leaf**：只依赖 stdlib（adapter 在方法内惰性 import），故 `tools/sandbox_backends`
的 adapter 可反过来 import 这里的 `SandboxPlan` 而不形成 import 环。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


# ─── 枚举 ────────────────────────────────────────────────────

class SandboxEngine(str, Enum):
    """策略选定的执行引擎语义（profile 字段）。"""

    AUTO = "auto"      # native 优先,严格/显式才升级 VM,永不降级 host
    NATIVE = "native"  # 强制 native OS sandbox
    VM = "vm"          # 强制 microVM
    HOST = "host"      # 宿主执行(danger-full-access);每条命令仍需审批


class SandboxBackend(str, Enum):
    """规划产出的具体后端（plan 字段）——adapter 据此执行。"""

    NATIVE = "native"
    MICROVM = "microvm"
    HOST = "host"


class NetworkMode(str, Enum):
    NONE = "none"
    ALLOWLIST = "allowlist"  # 第一版无后端能 enforce → 触发 deny（fail-closed）
    FULL = "full"


# capabilities 拥有的受保护元数据目录默认集（docs/19 §10.4）。
# 刻意与 tools/sandbox_backends/base.py 的 DEFAULT_PROTECTED_ROOTS 同值但**独立定义**：
# base 是 L0 adapter，被 tools.permissions import，不能反向依赖 L1 capabilities（避免 import 环）。
# 二者均列同一组众所周知的元数据目录；漂移风险极低（5 个固定目录名）。
DEFAULT_PROTECTED_ROOTS = (".git", ".nanocode", ".codex", ".agents", ".claude")


# ─── runtime 注入的上下文 ─────────────────────────────────────

@dataclass(frozen=True)
class HostContext:
    """runtime 注入的执行上下文——模型**绝不**能影响这里的任何字段（docs/19 §4.1）。

    cwd / session_id / workspace_roots / 身份 / interactive 都由宿主（Agent / runtime facade /
    后台 runner / hook 执行器）构造，不来自 tool input。
    """

    cwd: Path
    session_id: str
    workspace_roots: tuple[Path, ...]
    temp_roots: tuple[Path, ...]
    interactive: bool
    is_subagent: bool = False
    is_background: bool = False
    is_hook: bool = False
    approval_mode: str = "default"


@dataclass(frozen=True)
class ShellRequest:
    """public run_shell args 的解析结果（已过 validator + permission）。

    `stdin` 是 runtime 注入（hook event JSON 等），非模型参数；模型 schema 不含它。
    """

    command: str
    timeout_ms: int
    run_in_background: bool = False
    escalate: bool = False
    stdin: str | None = None

    @classmethod
    def from_tool_input(cls, inp: dict, *, default_timeout_ms: int = 30000) -> "ShellRequest":
        """validated run_shell inp → ShellRequest（timeout 以 ms 计）。

        前台缺省 30000ms；后台缺省 0（= 不设超时，由模型显式 timeout 覆盖）。
        """
        raw_timeout = inp.get("timeout")
        bg = bool(inp.get("run_in_background"))
        if raw_timeout is not None:
            try:
                timeout_ms = int(raw_timeout)
            except (TypeError, ValueError):
                timeout_ms = 0 if bg else default_timeout_ms
        else:
            timeout_ms = 0 if bg else default_timeout_ms
        return cls(
            command=str(inp.get("command") or ""),
            timeout_ms=timeout_ms,
            run_in_background=bg,
            escalate=bool(inp.get("escalate")),
            stdin=inp.get("stdin"),
        )


# ─── 策略（session/profile 投影）────────────────────────────────

@dataclass(frozen=True)
class FileSystemPolicy:
    """文件系统策略。语义：

    - writable_roots：可写目录（workspace + temp）。空 = read-only 姿态（仅 /dev/null 可写）。
    - protected_roots：落在 writable_roots 内仍**只读**的元数据目录绝对路径（含 .git pointer target）。
    - denied_roots：需**拒绝读取**的目录（strict）。native base profile 放行 file-read*，无法 deny
      指定读 → denied_roots 非空时 `native_can_enforce` 返回 False，AUTO 会升级到 VM（只挂 workspace,
      其余天然不可见 = 拒读）。
    - readable_roots：VM 专用的显式读挂载；native 整盘可读，忽略此字段（() = 读全盘）。
    """

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
    """session/profile 状态的不可变投影——SandboxManager 规划的策略输入。"""

    engine: SandboxEngine
    filesystem: FileSystemPolicy
    network: NetworkPolicy
    approval_mode: str
    vm_image: str = "python:3.12"
    vm_persist: bool = False
    # profile/上下文标记"需要更强隔离"（hook/subagent/strict 可设）→ AUTO 跳过 native 直取 VM。
    vm_required: bool = False
    profile: str = "default"


# ─── 规划产出 ─────────────────────────────────────────────────

@dataclass(frozen=True)
class SandboxPlan:
    """adapter 的**唯一输入**（adapter 不再读 raw dict）。"""

    backend: SandboxBackend
    command: str
    cwd: Path
    timeout_ms: int
    filesystem: FileSystemPolicy
    network: NetworkPolicy
    session_id: str
    env: tuple[tuple[str, str], ...] = ()
    stdin: str | None = None
    vm_image: str | None = None
    vm_name: str | None = None
    vm_fingerprint: str | None = None


@dataclass(frozen=True)
class SandboxDeny:
    """结构化拒绝（不是字符串）——docs/19 §5/§9：plan 返回结构化 deny。

    - reason：人面文案。
    - code：机器可判别原因（no_backend / vm_unavailable / native_unavailable /
      network_unenforceable / escalation_denied / host_not_allowed）。
    - escalation_hint：是否提示 `escalate=true` 重试到宿主（fail-closed 文案的一部分）。
    """

    reason: str
    code: str
    escalation_hint: bool = False


@dataclass(frozen=True)
class ApprovalDecision:
    """permission 层对一次"跨越沙盒边界"（escalate / host）请求的审批结论。

    non-interactive 上下文无法新审批 → approved 恒 False（fail-closed）。
    """

    approved: bool = False


# ─── 受保护 roots + .git pointer 解析（docs/19 §10.4）──────────────

def _resolve_gitdir_pointer(git_path: Path) -> Path | None:
    """若 `.git` 是文件且内容为 `gitdir: <path>`（worktree/submodule），返回 target 的 realpath。

    target 相对路径相对 `.git` 文件所在目录解析。非 gitfile / 解析失败 → None。
    """
    try:
        if not git_path.is_file():
            return None
        text = git_path.read_text(errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    target = text[len(prefix):].strip()
    if not target:
        return None
    base = str(git_path.parent)
    abs_target = target if os.path.isabs(target) else os.path.join(base, target)
    return Path(os.path.realpath(abs_target))


def protected_roots_for_workspace(root: Path) -> tuple[Path, ...]:
    """workspace 根下受保护元数据目录的绝对 realpath 集（含 .git pointer target）。"""
    root_real = Path(os.path.realpath(str(root)))
    out: list[Path] = []
    for name in DEFAULT_PROTECTED_ROOTS:
        p = root_real / name
        out.append(Path(os.path.realpath(str(p))))
        if name == ".git":
            target = _resolve_gitdir_pointer(root_real / name)
            if target is not None:
                out.append(target)
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return tuple(uniq)


# ─── Profiles（docs/19 §7.1）─────────────────────────────────────

PROFILES = ("default", "read-only", "strict", "vm", "danger-full-access")


def _ws_roots(host: HostContext) -> tuple[Path, ...]:
    roots = list(host.workspace_roots) or [host.cwd]
    return tuple(roots)


def _writable_default(host: HostContext) -> tuple[Path, ...]:
    """workspace + temp roots，realpath 去重。"""
    out: list[Path] = []
    seen: set[str] = set()
    for p in list(_ws_roots(host)) + list(host.temp_roots):
        rp = Path(os.path.realpath(str(p)))
        if str(rp) not in seen:
            seen.add(str(rp))
            out.append(rp)
    return tuple(out)


def _protected(host: HostContext) -> tuple[Path, ...]:
    out: list[Path] = []
    seen: set[str] = set()
    for r in _ws_roots(host):
        for p in protected_roots_for_workspace(r):
            if str(p) not in seen:
                seen.add(str(p))
                out.append(p)
    return tuple(out)


def policy_for_profile(profile: str, host: HostContext) -> SandboxPolicy:
    """profile 名 + HostContext → 不可变 SandboxPolicy（docs/19 §7.1）。

    未知 profile → 回退 default（不静默扩权；default 是最严的可用档）。
    """
    name = profile if profile in PROFILES else "default"
    writable = _writable_default(host)
    protected = _protected(host)
    none_net = NetworkPolicy(mode=NetworkMode.NONE)

    if name == "read-only":
        return SandboxPolicy(
            engine=SandboxEngine.NATIVE,
            filesystem=FileSystemPolicy(readable_roots=(), writable_roots=(),
                                        denied_roots=(), protected_roots=protected),
            network=none_net, approval_mode=host.approval_mode, profile=name)
    if name == "strict":
        # workspace-write + tighter deny reads（denied = temp 之外的 $HOME 等敏感读拒绝由 VM 兜底）。
        # 第一版用 vm_required 表达"更强隔离"，使 AUTO 升级 VM；denied_roots 留空（native 无法 deny
        # 读，VM 只挂 workspace 天然实现）。
        return SandboxPolicy(
            engine=SandboxEngine.AUTO,
            filesystem=FileSystemPolicy(readable_roots=writable, writable_roots=writable,
                                        denied_roots=(), protected_roots=protected),
            network=none_net, approval_mode=host.approval_mode, vm_required=True, profile=name)
    if name == "vm":
        return SandboxPolicy(
            engine=SandboxEngine.VM,
            filesystem=FileSystemPolicy(readable_roots=writable, writable_roots=writable,
                                        denied_roots=(), protected_roots=protected),
            network=none_net, approval_mode=host.approval_mode, vm_required=True, profile=name)
    if name == "danger-full-access":
        return SandboxPolicy(
            engine=SandboxEngine.HOST,
            filesystem=FileSystemPolicy(readable_roots=(), writable_roots=writable,
                                        denied_roots=(), protected_roots=()),
            network=NetworkPolicy(mode=NetworkMode.FULL),
            approval_mode="explicit", profile=name)
    # default：Codex 风格 workspace-write / network none / engine auto
    return SandboxPolicy(
        engine=SandboxEngine.AUTO,
        filesystem=FileSystemPolicy(readable_roots=(), writable_roots=writable,
                                    denied_roots=(), protected_roots=protected),
        network=none_net, approval_mode=host.approval_mode, profile=name)


def default_sandbox_policy(host: HostContext) -> SandboxPolicy:
    return policy_for_profile("default", host)


def narrow_policy_for_context(policy: SandboxPolicy, host: HostContext) -> SandboxPolicy:
    """据执行身份**收窄**策略（docs/19 §8）——只收窄，绝不放宽。

    hook / background 无法交互审批，绝不在宿主裸跑：engine=HOST（danger-full-access）收窄为
    AUTO（native-first）。**关键**：danger profile 的 filesystem.protected_roots=() / network=FULL
    不得带入受限上下文——下放的同时重建受限姿态（补回 workspace protected roots + 强制 network=none），
    否则被收窄回沙盒的 hook/background 仍能写 .git/hooks/* 并联网（review HIGH-1）。

    subagent 的 parent-narrowing 在 spawn 侧完成（继承父 effective policy 后再过此函数）。
    """
    if not (host.is_hook or host.is_background):
        return policy
    if policy.engine != SandboxEngine.HOST:
        return policy
    fs = policy.filesystem
    confined_fs = FileSystemPolicy(
        readable_roots=fs.readable_roots,
        writable_roots=fs.writable_roots or _writable_default(host),
        denied_roots=fs.denied_roots,
        protected_roots=fs.protected_roots or _protected(host))
    return SandboxPolicy(
        engine=SandboxEngine.AUTO, filesystem=confined_fs,
        network=NetworkPolicy(mode=NetworkMode.NONE),
        approval_mode=policy.approval_mode, vm_image=policy.vm_image,
        vm_persist=policy.vm_persist, vm_required=policy.vm_required, profile=policy.profile)


# ─── 后端能力判定 + 选择（docs/19 §7.2）─────────────────────────────

def native_can_enforce(policy: SandboxPolicy) -> bool:
    """native OS sandbox（seatbelt/bwrap）能否表达该策略。

    - network allowlist：seatbelt/bwrap 都做不到精细 allowlist → 不能。
    - denied_roots 非空：native base profile 放行 file-read*，无法 deny 指定读 → 不能。
    - 其余（read-only / workspace-write / network none|full）：能。
    """
    if policy.network.mode == NetworkMode.ALLOWLIST:
        return False
    if policy.filesystem.denied_roots:
        return False
    return True


def vm_can_enforce(policy: SandboxPolicy) -> bool:
    """microVM 能否表达该策略。

    第一版 microsandbox：network 仅 none / full（`--no-net` 或放行），无 allowlist → allowlist 不能。
    denied reads 由"只挂 workspace"天然实现 → 能。
    """
    if policy.network.mode == NetworkMode.ALLOWLIST:
        return False
    return True


def requires_vm(policy: SandboxPolicy) -> bool:
    """是否**必须**升级 VM（保守触发，docs/19 §7.2）：仅 profile/上下文显式标记 vm_required。

    复杂命令语义猜测（不可信代码启发式）第一版不做。
    """
    return policy.vm_required


def _unsandboxed_allowed(policy: SandboxPolicy) -> bool:
    """该策略是否允许 escalate 逃逸到宿主（host）执行。

    read-only profile（无可写根）不允许——逃逸到宿主全盘可写违背只读契约。其余允许（需审批）。
    """
    if policy.engine == SandboxEngine.HOST:
        return True
    return bool(policy.filesystem.writable_roots)


def choose_backend(
    policy: SandboxPolicy,
    request: ShellRequest,
    approval: ApprovalDecision,
    native_available: bool,
    vm_available: bool,
) -> SandboxBackend | None:
    """策略 × 请求 × 审批 × 后端可用性 → 后端（None = deny）。docs/19 §7.2 伪代码的实现。

    不变量：永不从沙盒自动降级到 host；escalate/host 永远需要明确审批。
    """
    # engine=host（danger-full-access）：宿主执行**仍需每条命令 escalate + approval**（docs/19 §7.2）。
    # profile 选择不是 blanket 授权——危险档命令也走 escalate confirm（与 default 档逃逸同一审批闸）。
    if policy.engine == SandboxEngine.HOST:
        return SandboxBackend.HOST if (request.escalate and approval.approved) else None

    # escalate：跨越沙盒到宿主——需 approved 且策略允许 unsandboxed。
    if request.escalate:
        if approval.approved and _unsandboxed_allowed(policy):
            return SandboxBackend.HOST
        return None

    if policy.engine == SandboxEngine.VM:
        return SandboxBackend.MICROVM if (vm_available and vm_can_enforce(policy)) else None

    if policy.engine == SandboxEngine.NATIVE:
        return SandboxBackend.NATIVE if (native_available and native_can_enforce(policy)) else None

    # AUTO：native 优先,严格/显式升级 VM,都不行 → deny（绝不降级 host）。
    if native_available and native_can_enforce(policy) and not requires_vm(policy):
        return SandboxBackend.NATIVE
    if vm_available and vm_can_enforce(policy):
        return SandboxBackend.MICROVM
    return None


# ─── SandboxManager ──────────────────────────────────────────────

_NO_BACKEND_HINT = (
    "Retry the SAME command with escalate=true to run it on the host "
    "(you will be asked to approve)."
)

# 命令在沙盒内正常跑但失败（非零退出/超时）时前置的提示——区别于机制失败。
_NATIVE_FAIL_HINT = (
    "[sandbox] This command ran in an OS sandbox (writes confined to the workspace, "
    "no network). If it failed because it needs network access or to write outside "
    "the workspace, retry the SAME command with escalate=true to run it on the host "
    "(you will be asked to approve)."
)
_VM_FAIL_HINT = (
    "[sandbox] This command ran in an isolated microVM (no network, workspace mounted). "
    "If it failed because it needs network access, host tools, or host filesystem access, "
    "retry the SAME command with escalate=true to run it on the host (you will be asked to approve)."
)


class SandboxManager:
    """shell 执行的唯一规划点 + 执行编排（docs/19 §5）。

    adapter（native seatbelt/bwrap、microVM）在方法内惰性 import 并接收 `SandboxPlan`。
    单测可注入 fake backend / probe，完全脱离真实沙盒可用性。
    """

    def __init__(
        self,
        *,
        native_backend=None,
        vm_adapter=None,
        native_probe=None,
        vm_probe=None,
    ) -> None:
        self._native_backend = native_backend
        self._vm_adapter = vm_adapter
        self._native_probe = native_probe
        self._vm_probe = vm_probe

    # ── 后端解析 / 可用性 ──
    def _native(self):
        if self._native_backend is not None:
            return self._native_backend
        from ..tools.sandbox_backends import resolve_native_backend
        return resolve_native_backend()

    def _vm(self):
        if self._vm_adapter is not None:
            return self._vm_adapter
        from ..tools.sandbox_backends import microsandbox
        return microsandbox

    def native_available(self) -> bool:
        if self._native_probe is not None:
            return self._native_probe()
        return self._native() is not None

    def vm_available(self) -> bool:
        if self._vm_probe is not None:
            return self._vm_probe()
        try:
            return bool(self._vm().is_available())
        except Exception:
            return False

    # ── 纯规划 ──
    def plan_shell(
        self,
        request: ShellRequest,
        host: HostContext,
        policy: SandboxPolicy,
        approval: ApprovalDecision,
    ) -> "SandboxPlan | SandboxDeny":
        native_avail = self.native_available()
        vm_avail = self.vm_available()
        backend = choose_backend(policy, request, approval, native_avail, vm_avail)
        if backend is None:
            return self._deny(policy, request, approval, native_avail, vm_avail)
        if backend is SandboxBackend.MICROVM:
            return SandboxPlan(
                backend=backend, command=request.command, cwd=host.cwd,
                timeout_ms=request.timeout_ms, filesystem=policy.filesystem,
                network=policy.network, session_id=host.session_id, stdin=request.stdin,
                vm_image=policy.vm_image, vm_name=_vm_name(host))
        return SandboxPlan(
            backend=backend, command=request.command, cwd=host.cwd,
            timeout_ms=request.timeout_ms, filesystem=policy.filesystem,
            network=policy.network, session_id=host.session_id, stdin=request.stdin)

    def _deny(
        self,
        policy: SandboxPolicy,
        request: ShellRequest,
        approval: ApprovalDecision,
        native_avail: bool,
        vm_avail: bool,
    ) -> SandboxDeny:
        if request.escalate and not approval.approved:
            return SandboxDeny(
                reason="host escalation was not approved (non-interactive context cannot approve)",
                code="escalation_denied")
        if request.escalate and not _unsandboxed_allowed(policy):
            return SandboxDeny(
                reason=f"escalate to host is not allowed under the {policy.profile} profile",
                code="host_not_allowed")
        if policy.network.mode == NetworkMode.ALLOWLIST:
            return SandboxDeny(
                reason="requested network policy (allowlist) cannot be enforced by any available backend",
                code="network_unenforceable")
        if policy.engine == SandboxEngine.VM:
            return SandboxDeny(
                reason="this profile requires a microVM but the microsandbox backend is unavailable",
                code="vm_unavailable")
        if policy.engine == SandboxEngine.NATIVE:
            return SandboxDeny(
                reason="native OS sandbox is unavailable on this host. " + _NO_BACKEND_HINT,
                code="native_unavailable", escalation_hint=True)
        # AUTO：native 与 VM 都不可用 / 不能 enforce
        return SandboxDeny(
            reason="no sandbox backend can enforce this policy on this host. " + _NO_BACKEND_HINT,
            code="no_backend", escalation_hint=True)

    # ── 执行（foreground / hook 文本；background 流式 dict）──
    #
    # approval 是**显式传入**的 ApprovalDecision，不在此推断（docs/19 §5 / review HIGH）：调用方
    # （Agent）在 permission gate 后构造它——escalate 抵达执行 ⟹ _authorize_dispatch 已批准；危险档
    # 同理。SandboxManager 不信任 model-controlled 的 escalate bit 自行决定上宿主，杜绝绕过 permission
    # 的直接调用者拿到宿主执行。

    async def execute_shell(
        self,
        request: ShellRequest,
        host: HostContext,
        policy: SandboxPolicy,
        approval: ApprovalDecision,
    ) -> str:
        """前台 / hook shell 执行：规划 → 调对应 backend → 格式化文本。"""
        plan = self.plan_shell(request, host, policy, approval)
        if isinstance(plan, SandboxDeny):
            return f"[sandbox] {plan.reason}"
        if plan.backend is SandboxBackend.HOST:
            return self._host_text(plan)
        if plan.backend is SandboxBackend.NATIVE:
            return self._native_text(plan)
        return self._vm_text(plan)

    async def execute_structured(
        self,
        request: ShellRequest,
        host: HostContext,
        policy: SandboxPolicy,
        approval: ApprovalDecision,
    ) -> dict:
        """规划 + 执行，返回结构化 dict（exit_code/stdout/stderr/timed_out/error；deny → 'blocked'）。

        供 hook 等需要结构化结果的入口使用。"""
        plan = self.plan_shell(request, host, policy, approval)
        if isinstance(plan, SandboxDeny):
            return {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False,
                    "error": None, "blocked": plan.reason}
        if plan.backend is SandboxBackend.NATIVE:
            backend = self._native()
            if backend is None:
                return {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False,
                        "error": None, "blocked": "native OS sandbox unavailable"}
            return backend.run_structured_plan(plan)
        if plan.backend is SandboxBackend.MICROVM:
            return self._vm().run_plan(plan)
        return _host_structured(plan)

    async def execute_background(
        self,
        request: ShellRequest,
        host: HostContext,
        policy: SandboxPolicy,
        approval: ApprovalDecision,
        *,
        stdout_path: str,
        stderr_path: str,
    ) -> dict:
        """后台 shell 执行：流式 stdout/stderr 到文件，返回结构化 dict。

        microVM 无法异步后台包裹 → blocked；deny → blocked（均不 spawn 子进程，fail-closed）。
        background 不支持 escalate（permission 层已拒 run_in_background+escalate），故 approval 恒不批。
        """
        plan = self.plan_shell(request, host, policy, approval)
        out = {"exit_code": None, "timed_out": False, "cancelled": False, "error": None}
        if isinstance(plan, SandboxDeny):
            out["blocked"] = plan.reason
            return out
        if plan.backend is SandboxBackend.MICROVM:
            out["blocked"] = ("background command is not available under the microVM sandbox — "
                              "run it in the foreground or add escalate=true to run on the host")
            return out
        Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
        Path(stderr_path).parent.mkdir(parents=True, exist_ok=True)
        proc = None
        try:
            with open(stdout_path, "wb") as fo, open(stderr_path, "wb") as fe:
                if plan.backend is SandboxBackend.NATIVE:
                    backend = self._native()
                    argv = backend.build_argv_from_plan(plan)
                    # cwd + TMPDIR 与前台 run_structured_plan 一致：seatbelt argv 无 --chdir，
                    # 不传 cwd 会在 launcher cwd 跑（review MED-8）。
                    env = dict(os.environ)
                    env["TMPDIR"] = os.environ.get("TMPDIR") or "/tmp"
                    proc = await asyncio.create_subprocess_exec(
                        *argv, stdout=fo, stderr=fe, cwd=str(plan.cwd), env=env)
                else:  # HOST
                    proc = await asyncio.create_subprocess_shell(
                        plan.command, stdout=fo, stderr=fe, cwd=str(plan.cwd))
                timeout_s = (plan.timeout_ms / 1000) if plan.timeout_ms else None
                try:
                    if timeout_s is not None:
                        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
                    else:
                        await proc.wait()
                    out["exit_code"] = proc.returncode
                except asyncio.TimeoutError:
                    out["timed_out"] = True
                    await _terminate_then_kill(proc)
        except asyncio.CancelledError:
            out["cancelled"] = True
            if proc is not None:
                await _terminate_then_kill(proc)
            raise
        except Exception as e:
            out["error"] = str(e)
        return out

    # ── backend 文本编排（机制失败 vs 命令失败 区分，但都不自动扩权）──

    def _native_text(self, plan: SandboxPlan) -> str:
        backend = self._native()
        if backend is None:
            return "[sandbox] native OS sandbox unavailable on this host. " + _NO_BACKEND_HINT
        r = backend.run_structured_plan(plan)
        if r["error"] is not None:
            return ("[sandbox] native OS sandbox failed to run this command "
                    f"({r['error']}). " + _NO_BACKEND_HINT)
        if r["timed_out"] or r["exit_code"] != 0:
            return f"{_NATIVE_FAIL_HINT}\n\n{_format_structured(r, plan.timeout_ms)}"
        return r["stdout"] or "(no output)"

    def _vm_text(self, plan: SandboxPlan) -> str:
        adapter = self._vm()
        r = adapter.run_plan(plan)
        if r.get("error") is not None:
            return ("[sandbox] microVM failed to run this command "
                    f"({r['error']}). " + _NO_BACKEND_HINT)
        if r["timed_out"] or r["exit_code"] != 0:
            return f"{_VM_FAIL_HINT}\n\n{_format_structured(r, plan.timeout_ms)}"
        return r["stdout"] or "(no output)"

    def _host_text(self, plan: SandboxPlan) -> str:
        r = _host_structured(plan)
        if r["error"] is not None:
            return f"Error: {r['error']}"
        return _format_structured(r, plan.timeout_ms)


def _format_structured(r: dict, timeout_ms: int) -> str:
    """结构化结果 → 文本（字节级对齐旧 run_shell.run 文案）。"""
    if r["timed_out"]:
        return f"Command timed out after {timeout_ms}ms"
    if r["exit_code"] != 0:
        stderr = f"\nStderr: {r['stderr']}" if r["stderr"] else ""
        stdout = f"\nStdout: {r['stdout']}" if r["stdout"] else ""
        return f"Command failed (exit code {r['exit_code']}){stdout}{stderr}"
    return r["stdout"] or "(no output)"


def _host_structured(plan: SandboxPlan) -> dict:
    """宿主裸跑 plan.command（仅 escalate/danger 经审批后抵达）。"""
    return exec_host_command(plan.command, cwd=str(plan.cwd),
                             timeout_ms=plan.timeout_ms, stdin=plan.stdin)


def exec_host_command(command: str, *, cwd: str, timeout_ms: int,
                      stdin: str | None = None) -> dict:
    """宿主上直接执行 shell 命令，返回结构化 dict。

    用于：(a) SandboxManager 的 HOST backend（escalate/danger，经审批）；(b) runtime 的显式
    user shell（`!` 命令，用户主动、非模型，不经沙盒）。timeout_ms 为假 → 不设超时。
    """
    out = {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "error": None}
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=(timeout_ms / 1000) if timeout_ms else None, input=stdin, cwd=cwd)
        out["exit_code"], out["stdout"], out["stderr"] = (r.returncode, r.stdout or "", r.stderr or "")
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
    except Exception as e:
        out["error"] = str(e)
    return out


async def _terminate_then_kill(proc, grace_s: float = 3.0) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


def _vm_name(host: HostContext) -> str:
    """microVM 名（来自 runtime session id，不来自模型）。第一版 ephemeral，仅用于诊断/命名。"""
    return f"nanocode-sbx-{host.session_id}"
