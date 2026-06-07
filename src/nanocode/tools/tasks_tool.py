"""task_list / task_output / task_stop 工具：schema + 文本渲染 + 停止逻辑。
执行需访问 Agent 持有的 TaskManager，故由 engine 分发（不进 execute_tool 表）。"""
from __future__ import annotations

import asyncio

from ..tasks.runner import tail_file
from ..tasks.models import TERMINAL_TASK_STATUSES

LIST_SCHEMA = {
    "name": "task_list",
    "description": "List background tasks. Optionally filter by status (running/completed/failed/...) or kind (shell).",
    "input_schema": {"type": "object", "properties": {
        "status": {"type": "string", "description": "Filter by status, e.g. 'running'"},
        "kind": {"type": "string", "description": "Filter by kind, e.g. 'shell'"}}, "required": []},
}
OUTPUT_SCHEMA = {
    "name": "task_output",
    "description": "Inspect a background task: status, summary, and a tail of its stdout/stderr logs.",
    "input_schema": {"type": "object", "properties": {
        "task_id": {"type": "string", "description": "The task id, e.g. 'task-001'"},
        "tail_bytes": {"type": "number", "description": "Bytes of log tail to show (default 8000)"}},
        "required": ["task_id"]},
}
STOP_SCHEMA = {
    "name": "task_stop",
    "description": "Stop a running background task (terminate then kill after grace).",
    "input_schema": {"type": "object", "properties": {
        "task_id": {"type": "string", "description": "The task id to stop"}}, "required": ["task_id"]},
}


def list_tasks_text(manager, status, kind) -> str:
    tasks = manager.list_tasks(status=status)
    if kind:
        tasks = [t for t in tasks if t.kind == kind]
    if not tasks:
        return "No tasks match the filter."
    return "\n".join(
        f"{t.id}  [{t.kind}]  {t.status}  exit={t.exit_code}  {t.description}"
        for t in sorted(tasks, key=lambda x: x.id))


def task_output_text(manager, task_id: str, tail_bytes: int = 8000) -> str:
    t = manager.get_task(task_id)
    if t is None:
        return f"Unknown task: {task_id}"
    parts = [f"Task {t.id} [{t.kind}] status={t.status} exit_code={t.exit_code}",
             f"Description: {t.description}", f"Summary: {t.result_summary or '(none)'}"]
    if t.error:
        parts.append(f"Error: {t.error}")
    parts.append("\nstdout tail:\n" + (tail_file(t.stdout_path, tail_bytes) if t.stdout_path else "" or "(empty)"))
    parts.append("\nstderr tail:\n" + (tail_file(t.stderr_path, tail_bytes) if t.stderr_path else "" or "(empty)"))
    parts.append("\nFull logs:")
    if t.stdout_path:
        parts.append(f"  stdout: {t.stdout_path}")
    if t.stderr_path:
        parts.append(f"  stderr: {t.stderr_path}")
    return "\n".join(parts)


def list_subagents_text(manager) -> str:
    subs = manager.list_subagents()
    if not subs:
        return "No sub-agents in this session."
    return "\n".join(
        f"{a.id}  [{a.type}]  {a.status}  {a.description}"
        for a in sorted(subs, key=lambda x: x.id))


def subagent_detail_text(manager, agent_id: str) -> str:
    a = manager.get_subagent(agent_id)
    if a is None:
        return f"Unknown sub-agent: {agent_id}"
    parts = [
        f"Sub-agent {a.id} [{a.type}] status={a.status}",
        f"Description: {a.description}",
        f"Provider/model: {a.provider}/{a.model}",
        f"Created: {a.created_at}  Updated: {a.updated_at}",
    ]
    if a.message_path:
        parts.append(f"Messages: {a.message_path}")
    if a.task_id:
        parts.append(f"Task: {a.task_id}")
    return "\n".join(parts)


async def task_stop(manager, background_tasks: set, task_id: str, grace_s: float = 3.0) -> str:
    t = manager.get_task(task_id)
    if t is None:
        return f"Unknown task: {task_id}"
    if t.status in TERMINAL_TASK_STATUSES:
        return f"Task {task_id} is already in terminal status: {t.status}."
    target = None
    for bg in background_tasks:
        if getattr(bg, "_nanocode_task_id", None) == task_id:
            target = bg
            break
    if target is None:
        manager.update_task(task_id, status="cancelled",
                            result_summary="(stop requested; no live coroutine found)")
        return f"Task {task_id}: no live coroutine found; marked cancelled."
    target.cancel()
    try:
        await asyncio.wait_for(_await_quietly(target), timeout=grace_s)
    except asyncio.TimeoutError:
        pass
    return f"Requested stop of task {task_id} (cancelled)."


async def _await_quietly(task) -> None:
    try:
        await task
    except BaseException:
        pass
