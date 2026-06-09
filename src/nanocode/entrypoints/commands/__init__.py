"""commands/ —— slash command client 层（docs/11）。

定位：填 docs/09:771-797 的「slash command client」格子，是 CLI/entrypoints 关注点，
**不是新的核心层**；它在已落地的 runtime/session（agent/{session,runtime}.py）之上。

本包暴露 CMD-P0 的类型契约：
- CommandResult —— 判别联合 Local | Prompt | Control（不是平铺布尔）
- CommandSpec   —— 命令元数据（带 kind 判别式）
- CommandContext—— handler 执行上下文
- Command/Handler—— registry 项
- Registry      —— 查找契约（most-specific-first + 落空→chat）
"""

from __future__ import annotations

from .types import (
    Command,
    CommandContext,
    CommandResult,
    CommandSpec,
    Control,
    Handler,
    Local,
    Prompt,
    Registry,
    produces_turn,
    should_exit,
)

__all__ = [
    "Command",
    "CommandContext",
    "CommandResult",
    "CommandSpec",
    "Control",
    "Handler",
    "Local",
    "Prompt",
    "Registry",
    "produces_turn",
    "should_exit",
]
