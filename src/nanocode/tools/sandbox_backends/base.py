"""本机 OS 沙盒后端的公共契约。当前实现：seatbelt（macOS）、bwrap（Linux）。"""

from __future__ import annotations

from typing import Protocol

READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
DANGER_FULL_ACCESS = "danger-full-access"

# 可写工作区内仍受保护、禁止写入的项目元数据目录
DEFAULT_PROTECTED_ROOTS = (".git", ".nanocode", ".claude", ".codex", ".agents")


class SandboxBackend(Protocol):
    """native OS 沙盒 adapter 契约（docs/19）：只消费 SandboxPlan，绝不接 raw model dict。

    SandboxManager 仅调 `run_structured_plan(plan)`（前台/hook）与 `build_argv_from_plan(plan)`
    （后台流式 exec）；二者从 plan 取 command/cwd/writable/protected/network，不读隐藏字段。
    """

    def is_available(self) -> bool: ...

    def run_structured_plan(self, plan) -> dict: ...

    def build_argv_from_plan(self, plan) -> list[str]: ...
