"""本机 OS 沙盒后端的公共契约。当前实现：seatbelt（macOS）。PR-5 将加 bwrap（Linux）。"""

from __future__ import annotations

from typing import Protocol

READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
DANGER_FULL_ACCESS = "danger-full-access"

POSTURES = (READ_ONLY, WORKSPACE_WRITE, DANGER_FULL_ACCESS)

# 可写工作区内仍受保护、禁止写入的项目元数据目录
DEFAULT_PROTECTED_ROOTS = (".git", ".nanocode", ".claude", ".codex", ".agents")


class SandboxBackend(Protocol):
    def is_available(self) -> bool: ...

    def run_structured(
        self, inp: dict, *, posture: str = WORKSPACE_WRITE, cwd: str | None = None
    ) -> dict: ...
