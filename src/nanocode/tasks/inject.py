"""后台任务完成后的自动回注：渲染 <system-reminder> + 收集待注入任务。"""
from __future__ import annotations

from .models import TERMINAL_TASK_STATUSES
from .runner import tail_file

_TAIL_BYTES = 2000


def render_task_reminder(task) -> str:
    # 子 agent 类 task（subagent / memory_*）：无 stdout 日志，但有 result.md（result_path）。
    # 渲染 result_path + summary，并丢掉对它毫无意义的空 stdout Output-tail。
    if task.result_path:
        return ("<system-reminder>\n"
                f"Background task {task.id} {task.status}.\n\n"
                f"Kind: {task.kind}\n"
                f"Description: {task.description}\n"
                f"Exit code: {task.exit_code}\n"
                f"Summary:\n{task.result_summary or '(none)'}\n\n"
                f"Full result:\n{task.result_path}\n"
                "(read_file the full result for findings + files touched.)\n"
                "</system-reminder>")
    tail = tail_file(task.stdout_path, _TAIL_BYTES) if task.stdout_path else ""
    return ("<system-reminder>\n"
            f"Background task {task.id} {task.status}.\n\n"
            f"Kind: {task.kind}\n"
            f"Description: {task.description}\n"
            f"Exit code: {task.exit_code}\n"
            f"Summary:\n{task.result_summary or '(none)'}\n\n"
            f"Output tail:\n{tail or '(empty)'}\n\n"
            f"Full logs:\n{task.stdout_path or '(no log)'}\n"
            "</system-reminder>")


def collect_pending_injections(manager) -> list:
    pending = [t for t in manager.list_tasks()
               if t.status in TERMINAL_TASK_STATUSES and not t.injected]
    return sorted(pending, key=lambda t: t.id)


def render_run_reminder(run) -> str:
    """子 agent run（A2：memory curator/eval 等 inject_summary=True 的后台 run）的完成提醒。

    权威记录在 child-session run_record（非 TaskManager）；按 duck-typed AgentRunRecord 字段
    渲染（run_id/agent_type/description/result_summary/result_path）。
    """
    return ("<system-reminder>\n"
            f"Background run {run.run_id} {run.status}.\n\n"
            f"Kind: {run.agent_type}\n"
            f"Description: {run.description}\n"
            f"Summary:\n{run.result_summary or '(none)'}\n\n"
            f"Full result:\n{run.result_path or '(none)'}\n"
            "(read_file the full result for the proposal + outcome.)\n"
            "</system-reminder>")
