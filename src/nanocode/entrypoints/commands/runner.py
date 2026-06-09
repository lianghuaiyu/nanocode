"""命令分发（CMD-P0，见 docs/11）。

`dispatch` 是纯分发器：`registry.lookup(line)` → 命中则 run handler 并返回其 `CommandResult`；
未命中返回 `NOT_A_COMMAND` 哨兵，由调用方（run_repl loop）回退到 skill 调用或普通 chat。

CMD-P0 刻意保持 1:1：handler 各自保留现有错误处理（如 /compact 的 try/except），runner
**不**新增会改变行为的统一 try/except。通用失败隔离（catch SystemExit/Exception、typed
AbortError）随 CMD-P2(/trace) 一起落地。KeyboardInterrupt / CancelledError 不被捕获，自然
向上传播（与今天一致）。

本模块**不 import cli**。
"""

from __future__ import annotations

from .registry import Registry, remainder
from .types import CommandContext

# 哨兵：registry 无匹配 → 非命令，调用方回退（skill / chat）。
NOT_A_COMMAND = object()


async def dispatch(line: str, registry: Registry, ctx: CommandContext):
    """返回 handler 的 CommandResult，或 NOT_A_COMMAND。`line` 应已 strip + 全角归一。"""
    cmd = registry.lookup(line)
    if cmd is None:
        return NOT_A_COMMAND
    args = remainder(line, cmd.spec)
    return await cmd.run(ctx, args)
