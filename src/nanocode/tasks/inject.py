"""后台任务完成后的自动回注：渲染 <system-reminder> + 收集待注入任务。"""
from __future__ import annotations

from .models import TERMINAL_TASK_STATUSES
from .runner import tail_file

_TAIL_BYTES = 2000


def render_task_reminder(task) -> str:
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
