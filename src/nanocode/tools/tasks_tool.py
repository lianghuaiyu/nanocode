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
    if t.result_path:
        parts.append(f"Result: {t.result_path}")
    parts.append("\nstdout tail:\n" + (tail_file(t.stdout_path, tail_bytes) if t.stdout_path else "" or "(empty)"))
    parts.append("\nstderr tail:\n" + (tail_file(t.stderr_path, tail_bytes) if t.stderr_path else "" or "(empty)"))
    parts.append("\nFull logs:")
    if t.stdout_path:
        parts.append(f"  stdout: {t.stdout_path}")
    if t.stderr_path:
        parts.append(f"  stderr: {t.stderr_path}")
    return "\n".join(parts)


def list_subagents_text(records) -> str:
    if not records:
        return "No sub-agent runs in this session."
    return "\n".join(
        f"{r.child_session_id}  [{r.agent_type}]  {r.status}  {r.description}  {r.summary or ''}".rstrip()
        for r in sorted(records, key=lambda x: x.child_session_id))


def subagent_detail_text(record) -> str:
    if record is None:
        return "Unknown sub-agent run."
    parts = [
        f"Sub-agent run {record.child_session_id} [{record.agent_type}] status={record.status}",
        f"Description: {record.description}",
        f"Parent session: {record.parent_session_id}",
        f"Context/isolation: {record.context_mode}/{record.isolation}",
        f"Provider/model: {(record.model or {}).get('provider')}/{(record.model or {}).get('modelId')}",
        f"Created: {record.created_at}  Started: {record.started_at}  Ended: {record.ended_at}",
    ]
    if record.worktree_path:
        parts.append(f"Worktree: {record.worktree_path}")
    if record.result_path:
        parts.append(f"Result: {record.result_path}")
    if record.error:
        parts.append(f"Error: {record.error}")
    return "\n".join(parts)


def list_agent_definitions_text(manager=None) -> str:
    """渲染"可用 agent 定义"目录：内置(explore/plan/general) + 每个自定义 agent。

    每行：name — one-line description；自定义额外附 source 路径与 model（若设）。
    与运行实例(list_subagents_text)互补——这是"定义"而非"实例"。
    """
    from ..agents.registry import (
        build_profile, discover_custom_agents, get_available_agent_types,
    )
    types = get_available_agent_types()
    custom = discover_custom_agents()
    lines = ["Available agent definitions:"]
    for t in types:
        name = t["name"]
        desc = (t.get("description") or "").strip() or "(no description)"
        line = f"  {name}  —  {desc}"
        extra = []
        if name in custom:
            profile = build_profile(name)
            if profile.model:
                extra.append(f"model={profile.model}")
            if profile.source:
                extra.append(f"source={profile.source}")
        if extra:
            line += "  [" + ", ".join(extra) + "]"
        lines.append(line)
    return "\n".join(lines)


def agent_definition_detail_text(name: str) -> str | None:
    """渲染单个 agent 定义的详情（source / extends / 有效工具 / disallowed / model /
    系统提示词预览）。若 name 不是已知定义则返回 None（调用方再尝试当 instance id）。"""
    from ..agents.registry import (
        RESERVED_AGENT_TYPES, build_profile, discover_custom_agents,
        effective_tools, get_available_agent_types,
    )
    known = {t["name"] for t in get_available_agent_types()}
    if name not in known or name in RESERVED_AGENT_TYPES:
        return None
    profile = build_profile(name)
    custom = discover_custom_agents().get(name)
    tool_names = sorted(t["name"] for t in effective_tools(profile))
    disallowed = sorted(profile.tools_deny)
    # allow-list 显示语义与旧 dict API 一致：无 allow-list → unrestricted；有 → **有效**名集
    # （deny 与 agent 剔除后的真实集合，即 call-time 强制的那一份）。
    allowed = None if profile.tools_allow is None else set(tool_names)
    parts = [
        f"Agent definition: {name}",
        f"Description: {(custom.get('description') if custom else '') or '(built-in)'}",
        f"Source: {profile.source or '(built-in)'}",
        f"Extends: {(custom.get('extends') if custom else None) or '(none)'}",
        f"Model: {profile.model or '(inherits parent)'}",
        f"Effective tools ({len(tool_names)}): {', '.join(tool_names) or '(none)'}",
        f"Allow-list: {'(unrestricted except agent)' if allowed is None else ', '.join(sorted(allowed)) or '(none)'}",
        f"Disallowed tools: {', '.join(disallowed) or '(none)'}",
    ]
    if profile.max_turns:
        parts.append(f"Max turns: {profile.max_turns}")
    if profile.timeout_ms:
        parts.append(f"Timeout (ms): {profile.timeout_ms}")
    preview = (profile.prompt or "").strip().replace("\n", " ")
    if len(preview) > 200:
        preview = preview[:200] + "…"
    parts.append(f"System-prompt preview: {preview or '(empty)'}")
    parts.append(
        "Note: tool restriction is ENFORCED at call time — a sub-agent of this "
        "type can only invoke the effective tools listed above; any other real "
        "tool call is blocked.")
    return "\n".join(parts)


def agents_overview_text(records) -> str:
    """`/agents`（无参）总览：可用定义 + 运行实例两段。"""
    return (list_agent_definitions_text()
            + "\n\nRunning instances:\n"
            + list_subagents_text(records))


async def task_stop(manager, background_tasks: set, task_id: str, grace_s: float = 3.0,
                    allow_orphan_cancel: bool = True) -> str:
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
        # 协程不在调用方持有的 background_tasks 内：
        # - 主 agent（allow_orphan_cancel=True，历史行为）：仍标 cancelled。
        # - 子 agent（False）：拒绝——不得 cancel 自己不持有的（父/兄弟）共享 task。
        if not allow_orphan_cancel:
            return (f"Task {task_id}: not owned by this sub-agent; refusing to stop "
                    f"a task it does not hold.")
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
