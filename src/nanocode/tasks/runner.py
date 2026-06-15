"""后台 shell 任务执行包装：把 SandboxManager.execute_background 的结果落进 TaskManager（docs/19）。

后台 shell 与前台共用唯一规划点 SandboxManager；本模块只负责把结构化结果落库为任务状态。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

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


async def run_shell_background_task(manager, sandbox, task_id, request, host, policy,
                                    approval, stdout_path, stderr_path) -> None:
    """后台 shell：经 SandboxManager.execute_background 流式执行并落库（docs/19）。

    request/host/policy/approval 由 engine 在 spawn 时构造（HostContext(is_background=True)，
    approval 不批——background 不支持 escalate）；microVM / 无后端 / deny → blocked（fail-closed）。
    """
    try:
        r = await sandbox.execute_background(
            request, host, policy, approval, stdout_path=stdout_path, stderr_path=stderr_path)
    except asyncio.CancelledError:
        manager.update_task(task_id, status="cancelled", result_summary="(cancelled by task_stop)")
        raise
    if r.get("blocked"):
        # fail-closed：后台命令无法关进沙盒（microVM / 无原生后端 / deny）→ 落库 blocked。
        manager.update_task(task_id, status="blocked", result_summary=r["blocked"])
        return
    status = classify_exit(r["exit_code"], r["timed_out"], r["cancelled"], r["error"])
    manager.update_task(task_id, status=status, exit_code=r["exit_code"],
                        result_summary=_summarize(stdout_path), error=r["error"])
