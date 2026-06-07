"""后台 shell 任务执行包装：把 run_shell.run_background 的结果落进 TaskManager。"""
from __future__ import annotations

import asyncio
from pathlib import Path

# 注意：run_shell 在函数内惰性导入。顶层 `from ..tools import run_shell` 会触发
# tools 包 __init__ → registry → tasks_tool → 回头 import 本模块的 tail_file，
# 形成循环导入（单独 import nanocode.tasks.runner 时即崩）。惰性导入打断该环。

_SUMMARY_CHARS = 500


def tail_file(path, tail_bytes: int) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    return data[-tail_bytes:].decode("utf-8", errors="replace")


def classify_exit(exit_code, timed_out: bool, cancelled: bool, error) -> str:
    if cancelled:
        return "cancelled"
    if timed_out:
        return "timed_out"
    if error is not None:
        return "failed"
    return "completed" if exit_code == 0 else "failed"


def _summarize(stdout_path: str) -> str:
    tail = tail_file(stdout_path, _SUMMARY_CHARS).strip()
    if not tail:
        return "(no stdout)"
    lines = tail.splitlines()
    return (lines[-1] if lines else tail)[:_SUMMARY_CHARS]


async def run_shell_background_task(manager, task_id, command, stdout_path, stderr_path, timeout_ms=None) -> None:
    from ..tools import run_shell  # 惰性导入，打破 tools ↔ tasks 循环
    inp = {"command": command}
    if timeout_ms is not None:
        inp["timeout"] = timeout_ms
    try:
        r = await run_shell.run_background(inp, stdout_path=stdout_path, stderr_path=stderr_path)
    except asyncio.CancelledError:
        manager.update_task(task_id, status="cancelled", result_summary="(cancelled by task_stop)")
        raise
    status = classify_exit(r["exit_code"], r["timed_out"], r["cancelled"], r["error"])
    manager.update_task(task_id, status=status, exit_code=r["exit_code"],
                        result_summary=_summarize(stdout_path), error=r["error"])
