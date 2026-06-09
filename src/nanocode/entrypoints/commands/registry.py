"""命令查找（CMD-P0，见 docs/11）。

most-specific-first：按 `spec.name` 长度降序匹配，故 `/memory eval generate` 先于
`/memory eval` 先于 `/memory`，`/task-stop` 先于 `/task`。`lookup` 对「不是已知命令」返回
`None`，由 runner/loop 回退到 skill 或 chat（保留未知 `/foo` 当文本发给模型的行为）。

本模块**不 import cli**，也不含领域逻辑——只做名字匹配。
"""

from __future__ import annotations

from .types import Command, CommandSpec


class Registry:
    def __init__(self) -> None:
        self._commands: list[Command] = []
        self._sorted: list[Command] = []

    def register(self, command: Command) -> None:
        self._commands.append(command)
        # 长度降序、同长按注册序稳定 —— 保证 most-specific-first。
        self._sorted = sorted(
            self._commands, key=lambda c: len(c.spec.name), reverse=True
        )

    def lookup(self, line: str) -> "Command | None":
        """返回匹配 `line` 的命令；非命令返回 None。`line` 应已 strip + 全角归一。"""
        for cmd in self._sorted:
            name = cmd.spec.name
            m = cmd.spec.match
            if m == "exact":
                if line == name:
                    return cmd
            elif m == "prefix":
                if line.startswith(name + " "):
                    return cmd
            else:  # exact_or_prefix
                if line == name or line.startswith(name + " "):
                    return cmd
        return None

    def specs(self) -> list[CommandSpec]:
        """注册序的全部 spec（供补全 / --help，CMD-P1）。"""
        return [c.spec for c in self._commands]


def remainder(line: str, spec: CommandSpec) -> str:
    """命令名之后的原始参数串（已 strip）；`line == name` 时为空串。"""
    name = spec.name
    if line.startswith(name + " "):
        return line[len(name) + 1:].strip()
    return ""
