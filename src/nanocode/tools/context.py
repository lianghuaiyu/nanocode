"""tools/context.py — ToolContext + 文件系统能力把手（docs/24 §4.2 / Phase 2 + 2b）。

per-call 的密封能力面：工具的 `run(ctx, inp)` 只能用 `ctx` 里**已铸造**的把手做事，物理上够不到
raw `Agent` / `_session_mgr` / `lease`（边界命根子，docs/24 §8.1）。Phase 2 只长 fs 三槽
（fs_read/fs_write/fs_list）；exec/spawn/memory/tasks/session/models/set_mode 等槽 Phase 3/4 再补。

能力把手由 dispatch 咽喉点（engine._run_real_tool）按「tool.needs ∩ 信任档策略」现场铸造：
- 内置（BUILTIN）声明什么铸什么；
- protected-root 写策略由咽喉点权限层独占（confirm/deny），把手内**不**重复拦 protected——
  否则会覆盖用户审批。

**Phase 2b（有意行为变更）**：写把手强制 `writable_roots` containment——`write_text`/`mkdir`
在落盘前，把目标 realpath 校验为落在 `policy.writable_roots` 之一内，否则抛 `PermissionError`。
语义随 profile：read-only 档 writable_roots=() → 所有写被拒；danger-full-access 档 writable_roots
取 `UNRESTRICTED` 哨兵 → 不拦（全宿主可写，与同档 shell 在宿主裸跑对齐）。
一切以咽喉点注入的 `sandbox_policy().filesystem.writable_roots` 为准。

`default_tool_context()` 只供显式的直接工具调用 / 单测使用，采用 `UNRESTRICTED` 哨兵，跳过
containment——它不是 read-only 档，而是调用方主动选择的宽松本地上下文。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..capabilities.sandbox import UNRESTRICTED, FileSystemPolicy


# `writable_roots` 取 `UNRESTRICTED`（capabilities.sandbox 的类型内哨兵单例）= 不强制 containment。
# 两种来源：(1) danger-full-access 档（全宿主可写，与同档 shell 对齐）；(2) `default_tool_context()`
# 显式直接调用上下文。受限档铸真 roots 元组（含空元组 = read-only 拒所有写），
# `_check_writable` 据 `is UNRESTRICTED` 判别跳过——哨兵是类型而非字符串，误路由会被下游 fail-loud。


def _is_within(abs_path: str, root: str) -> bool:
    """与 permissions._is_protected_path / SandboxManager 同源的 containment 惯用式。

    `root` 须已 realpath；`abs_path` 须已 realpath。`abs == root` 或 `abs.startswith(root + sep)`。
    """
    return abs_path == root or abs_path.startswith(root + os.sep)


# ─── 文件系统能力把手（沙箱中介；用一个 FileSystemPolicy 构造）─────────────────


@dataclass(frozen=True)
class FsReadCap:
    """读能力把手。Phase 2：读保持宽松（native 整盘可读）。

    Phase 2b 顺手补 denied_roots 拦截（默认 / workspace-write 档 denied_roots 通常为空 → 影响极小；
    仅在策略显式列出 deny 读目录时拒读）。与写的 containment 同源惯用式。
    """

    policy: FileSystemPolicy

    def _check_readable(self, path: str) -> None:
        denied = self.policy.denied_roots
        if not denied:
            return
        try:
            abs_path = os.path.realpath(path)
        except OSError:
            return
        real_denied = [os.path.realpath(str(r)) for r in denied]
        if any(_is_within(abs_path, r) for r in real_denied):
            raise PermissionError(
                f"read denied: {abs_path!r} is inside a denied root"
            )

    def read_bytes(self, path: str) -> bytes:
        self._check_readable(path)
        return Path(path).read_bytes()

    def read_text(self, path: str, *, errors: str = "strict") -> str:
        self._check_readable(path)
        if errors == "strict":
            return Path(path).read_text()
        return Path(path).read_text(errors=errors)


@dataclass(frozen=True)
class FsWriteCap:
    """写能力把手。**Phase 2b：强制 writable_roots containment（有意行为变更）。**

    `write_text` / `mkdir` 落盘前，把目标 realpath 校验为落在 `policy.writable_roots` 之一内
    （含相等或子路径），否则抛 `PermissionError`（错误信息含被拒路径 + 允许的 roots）。语义随 profile：
    - read-only 档 `writable_roots=()` → 任何写被拒；
    - default / workspace-write 档 → 仅 workspace + temp 可写；
    - danger-full-access 档 `writable_roots=UNRESTRICTED` 哨兵 → 不拦（全宿主可写，与同档 shell 对齐）。

    哨兵 `writable_roots is UNRESTRICTED`（danger-full-access 档 + `default_tool_context()`）
    → 跳过 containment，与历史裸写等价（非 read-only 档）。

    protected-root 写策略**由咽喉点独占**（permissions.check_permission：默认/acceptEdits/bypass
    下 protected 写映射为 confirm，dontAsk 映射为 deny）。把手内**不**重复拦 protected——否则会把
    咽喉点放行的「用户已确认 / bypass」protected 写翻成 PermissionError，覆盖用户审批。containment
    与 protected 正交：containment 把写**收**进 writable_roots，protected 把 writable_roots **内**的
    元数据目录排除——二者由不同层负责。
    """

    policy: FileSystemPolicy

    def _check_writable(self, path: str) -> None:
        roots = self.policy.writable_roots
        if roots is UNRESTRICTED:
            return
        try:
            abs_path = os.path.realpath(path)
        except OSError as e:  # pragma: no cover - realpath 几乎不抛
            raise PermissionError(f"cannot resolve write target {path!r}: {e}") from e
        real_roots = [os.path.realpath(str(r)) for r in roots]
        if any(_is_within(abs_path, r) for r in real_roots):
            return
        allowed = ", ".join(real_roots) if real_roots else "(none — read-only)"
        raise PermissionError(
            f"write denied: {abs_path!r} is outside the writable roots [{allowed}]"
        )

    def write_text(self, path: str, content: str) -> None:
        self._check_writable(path)
        Path(path).write_text(content)

    def mkdir(self, path: str, *, parents: bool = True, exist_ok: bool = True) -> None:
        self._check_writable(path)
        Path(path).mkdir(parents=parents, exist_ok=exist_ok)

    def read_text(self, path: str) -> str:
        """edit_file 需先读再写——读经写把手时同样宽松（与今天等价）。"""
        return Path(path).read_text()


@dataclass(frozen=True)
class FsListCap:
    """目录列举能力把手。Phase 2：列举保持宽松（与今天等价）。"""

    policy: FileSystemPolicy

    def listdir(self, path: str) -> list[str]:
        return os.listdir(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def is_dir(self, path: str) -> bool:
        # 与今天 list_files 的 per-entry `Path(full_path).is_dir()` **严格等价**：
        # 用 pathlib 语义（坏 symlink / 不存在 → False；ELOOP/EACCES/ENAMETOOLONG 等真实
        # stat 错误 → 抛 OSError），让 list_files 的 `except OSError: continue` 仍能丢弃坏条目。
        # 不可用 os.path.isdir（它吞掉所有 OSError → 坏条目会被当普通文件列出，属行为变更）。
        return Path(path).is_dir()


# ─── host-routed 能力把手（docs/24 §4.4 Phase 3：薄转发到 host 服务方法）─────────────
#
# 这些把手是**独立对象**，内部各持一个 host 引用做转发；但 ToolContext 本身不暴露任何通向
# raw Agent / _session_mgr / lease 的字段——工具只见把手，够不到底层（边界命根子）。把手语义
# 与今天 router 的 host-routed 分支**逐项等价**（纯转发，不加宽、不收窄）。


@dataclass(frozen=True)
class ExecCap:
    """EXEC 能力把手：run_shell 前台执行（唯一规划点 SandboxManager）。

    `run(request)` 包 (SandboxManager, host_context(), sandbox_policy(), approval)：approval 显式
    据 request.escalate 构造——escalate 抵达此处 ⟹ 咽喉点 _authorize_dispatch 已 gate 并批准。
    """

    _host: Any

    async def run(self, request: Any) -> str:
        from ..capabilities.sandbox import ApprovalDecision
        approval = ApprovalDecision(approved=request.escalate)
        return await self._host._sandbox.execute_shell(
            request, self._host.host_context(), self._host.sandbox_policy(), approval)


@dataclass(frozen=True)
class TasksCap:
    """TASKS 能力把手：后台任务面板（薄转发 host.list_tasks/task_output/stop_task/
    spawn_background_shell）。"""

    _host: Any

    def list(self, status=None, kind=None) -> str:
        return self._host.list_tasks(status, kind)

    def output(self, task_id: str, tail_bytes: int = 8000) -> str:
        return self._host.task_output(task_id, tail_bytes)

    async def stop(self, task_id: str) -> str:
        return await self._host.stop_task(task_id)

    async def spawn_shell(self, command: str, timeout_ms) -> str:
        return await self._host.spawn_background_shell(command, timeout_ms)


@dataclass(frozen=True)
class RunsCap:
    """SESSION_READ 能力把手：child-session run record 查询/操控（薄转发 host.run_*）。"""

    _host: Any

    def list(self, status: str | None = None) -> str:
        return self._host.run_list(status)

    def status(self, child_session_id: str) -> str:
        return self._host.run_status(child_session_id)

    def output(self, child_session_id: str, include_events: bool = False,
               tail_events: int = 20) -> str:
        return self._host.run_output(child_session_id, include_events, tail_events)

    async def cancel(self, child_session_id: str) -> str:
        return await self._host.run_cancel(child_session_id)

    def send(self, child_session_id: str, prompt: str, *,
             delivery: str = "steer") -> str:
        return self._host.run_send(child_session_id, prompt, delivery=delivery)


@dataclass(frozen=True)
class MemoryCap:
    """MEMORY 能力把手：薄转发 host.execute_memory_tool（consolidate 的 host 回调留在 service 内）。"""

    _host: Any

    async def execute(self, inp: dict) -> str:
        return await self._host.execute_memory_tool(inp)


@dataclass(frozen=True)
class SpawnCap:
    """SPAWN 能力把手：**纯转发** host.execute_agent_tool / execute_skill_tool。

    严禁加宽——编排逻辑留在 runtime/spawn.py、engine，绝不搬进把手。
    """

    _host: Any

    async def agent(self, inp: dict) -> str:
        return await self._host.execute_agent_tool(inp)

    async def skill(self, inp: dict) -> str:
        return await self._host.execute_skill_tool(inp)


@dataclass(frozen=True)
class SetModeCap:
    """SET_MODE 能力把手：plan-mode 状态切换（转发 PlanModeMixin via host.execute_plan_mode_tool）。

    **主 agent 专用**——子 agent 时 ToolContext.set_mode 槽为 None（保住主 agent-only 门）。
    host.execute_plan_mode_tool 对 enter/exit 都是 async（enter 仅切状态、exit 含交互审批），
    故两入口统一 async 转发。
    """

    _host: Any

    async def enter_plan(self) -> str:
        return await self._host.execute_plan_mode_tool("enter_plan_mode")

    async def exit_plan(self) -> str:
        return await self._host.execute_plan_mode_tool("exit_plan_mode")


# ─── per-call 上下文 ──────────────────────────────────────────────


@dataclass(frozen=True)
class ToolContext:
    """per-call 核心 + 按需铸造的能力把手（docs/24 §4.2 / §4.4）。

    **无任何字段通向 raw Agent / _session_mgr / lease**——这是嵌入式边界的命根子。把手是独立对象，
    内部可持 host 引用转发，但 ToolContext 本身不新增任何 raw 字段：工具只见把手。
    fs_read/fs_write/fs_list（Phase 2）+ exec/tasks/runs/memory/spawn/set_mode（Phase 3）；
    未授予的槽为 None。MODELS 槽 Phase 5。
    """

    call_id: str = ""
    cwd: str = ""
    signal: Any = None  # AbortSignal（若易取）；多为 None
    fs_read: "FsReadCap | None" = None
    fs_write: "FsWriteCap | None" = None
    fs_list: "FsListCap | None" = None
    exec: "ExecCap | None" = None
    tasks: "TasksCap | None" = None
    runs: "RunsCap | None" = None
    memory: "MemoryCap | None" = None
    spawn: "SpawnCap | None" = None
    set_mode: "SetModeCap | None" = None


def default_tool_context() -> "ToolContext":
    """显式直接工具调用用的宽松 fs ToolContext。

    用于不经咽喉点的直接调用。**不收紧**：writable_roots 取 `UNRESTRICTED` 哨兵
    （FsWriteCap 跳过 containment）；denied_roots 空（FsReadCap 不拦）。
    真实派发由 engine._mint_tool_context 按 needs 用 `sandbox_policy().filesystem` 铸造（带真 roots）。
    """
    policy = FileSystemPolicy(readable_roots=(), writable_roots=UNRESTRICTED,
                              denied_roots=(), protected_roots=())
    return ToolContext(
        fs_read=FsReadCap(policy),
        fs_write=FsWriteCap(policy),
        fs_list=FsListCap(policy),
    )
