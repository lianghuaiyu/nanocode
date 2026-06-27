"""orchestration/policy.py — chain/parallel 编排策略（layer④，docs/26 §0.6 阶段1）。

从内核 `runtime/spawn.py` 上提：序列化 chain（{previous} 串接）、parallel fan-in、前/后台、
fail-stop、all-or-nothing 预校验、整组 cancel。**不持** spawn 机器——成员一律经受信槽
`ctx.spawn`（子工具/sandbox 由内核派生，扩展不可提权）。三类成员原语逐一对应内核三原语：
fg 成员→`ctx.spawn.run_fresh`(bounded envelope)、后台 chain 步→`ctx.spawn.run_step`(await+group/
inject)、后台 parallel 成员→`ctx.spawn.run_background`(detached)。纯助手（类型归一/上下文投影）
从 `runtime.spawn` import（无 host 状态）。
"""
from __future__ import annotations

import asyncio

from ...runtime.spawn import _context_mode, _normalize_agent_type, _project_prompt

MAX_CHAIN_STEPS = 10
MAX_PARALLEL_TASKS = 8
PREVIOUS_PLACEHOLDER = "{previous}"


def _validate_items(items, *, what: str, cap: int) -> "str | None":
    if not isinstance(items, list) or not items:
        return f"Error: '{what}' must be a non-empty array of {{type?, description?, prompt}} objects."
    if len(items) > cap:
        return f"Error: too many {what} ({len(items)} > {cap})."
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict) or not str(it.get("prompt") or "").strip():
            return f"Error: {what}[{i}] must be an object with a non-empty 'prompt'."
    return None


def _bg_timeout(agent_type: str, item: dict, payload: dict):
    """后台成员 wall-clock 超时：item > tool 入参 > manifest profile > settings background_timeout_ms。"""
    from ...agents.registry import build_profile
    from ...tools import load_agents_config
    t = item.get("timeout_ms") or payload.get("timeout_ms")
    if t is None:
        t = build_profile(agent_type).timeout_ms
    if t is None:
        t = load_agents_config().get("background_timeout_ms")
    return t


async def orchestrate(ctx, payload: dict) -> str:
    """编排入口（host.run_orchestrator 调用）。steps→chain；tasks→parallel；run_in_background 分前/后台。

    steps⊥tasks 与 steps/tasks⊥resume/steer 的早期形状校验已在内核 execute_agent_tool 做过。"""
    if ctx.spawn is None:
        return "Error: orchestration spawn capability is unavailable."
    steps = payload.get("steps")
    if steps is not None:
        return await _chain(ctx, payload, steps)
    return await _parallel(ctx, payload, payload.get("tasks"))


# ─── chain ───────────────────────────────────────────────────────────────────

async def _chain(ctx, payload: dict, steps) -> str:
    err = _validate_items(steps, what="steps", cap=MAX_CHAIN_STEPS)
    if err:
        return err
    n = len(steps)
    if payload.get("run_in_background"):
        return await _chain_background(ctx, payload, steps, n)
    return await _chain_foreground(ctx, payload, steps, n)


async def _chain_foreground(ctx, payload: dict, steps, n: int) -> str:
    """按序跑 N 个独立子；{previous} 替换为上一步 bounded envelope；turn abort 在步边界生效。"""
    previous = "(no previous step)"
    sections: list[str] = []
    for i, step in enumerate(steps, 1):
        if ctx.thread.is_turn_aborted():
            sections.append(f"## Step {i}/{n} — skipped (turn aborted)")
            break
        agent_type = _normalize_agent_type(step.get("type", "general"))
        description = step.get("description") or f"chain step {i}/{n}"
        step_context = step.get("context") or {"mode": "fresh"}
        try:
            step_context_mode = _context_mode(step_context)
            prompt = _project_prompt(
                str(step["prompt"]).replace(PREVIOUS_PLACEHOLDER, previous), step_context)
        except ValueError as e:
            return f"Error: {e}"
        try:
            envelope = await ctx.spawn.run_fresh(
                agent_type, prompt, description=description,
                timeout_ms=step.get("timeout_ms") or payload.get("timeout_ms"),
                context_mode=step_context_mode, isolation=step.get("isolation"),
                parallel=False)
        except Exception as e:  # noqa: BLE001 — 成员错误归一为字符串（run_fresh 本身已归一）
            envelope = f"Sub-agent error: {e}"
        previous = envelope
        sections.append(f"## Step {i}/{n} [{agent_type}] {description}\n{envelope}")
    return "\n\n".join(sections)


async def _chain_background(ctx, payload: dict, steps, n: int) -> str:
    """detached coordinator 顺序跑 N 步；fail-stop；CancelledError 重抛供整组 cancel 级联。
    coordinator 自身不持 run_record（步骤才是 run）。all-or-nothing 预校验所有步。"""
    for i, step in enumerate(steps, 1):
        step_context = step.get("context") or {"mode": "fresh"}
        try:
            _context_mode(step_context)
            _project_prompt(
                str(step["prompt"]).replace(PREVIOUS_PLACEHOLDER, "(previous)"), step_context)
        except ValueError as e:
            return f"Error: steps[{i}]: {e}"
    gid = ctx.spawn.new_group()

    async def _coordinator() -> None:
        previous = "(no previous step)"
        for i, step in enumerate(steps, 1):
            agent_type = _normalize_agent_type(step.get("type", "general"))
            description = step.get("description") or f"chain step {i}/{n}"
            step_context = step.get("context") or {"mode": "fresh"}
            step_context_mode = _context_mode(step_context)
            prompt = _project_prompt(
                str(step["prompt"]).replace(PREVIOUS_PLACEHOLDER, previous), step_context)
            try:
                text = await ctx.spawn.run_step(
                    agent_type, prompt, group_id=gid, description=description,
                    inject_summary=True, result_summary=f"chain step {i}/{n}: {description}",
                    timeout_ms=_bg_timeout(agent_type, step, payload),
                    context_mode=step_context_mode)
            except asyncio.CancelledError:
                raise   # 在飞步已写 cancelled；重抛供 run_cancel 级联
            except Exception as e:  # noqa: BLE001 — 步失败 fail-stop（步已写终态 record）
                ctx.events.notice(
                    f"Background chain group {gid} stopped at step {i}/{n} ({description}): {e}",
                    level="warn")
                return
            previous = text
        ctx.events.notice(f"Background chain group {gid} completed all {n} step(s).")

    ctx.spawn.launch_coordinator(_coordinator(), group_id=gid)
    return (f"Started background chain group {gid} with {n} step(s). Steps run sequentially "
            f"({{previous}} threaded); each reports completion later (summary auto-injected). "
            f"Cancel the whole chain: run_cancel {gid}")


# ─── parallel ──────────────────────────────────────────────────────────────────

async def _parallel(ctx, payload: dict, tasks) -> str:
    err = _validate_items(tasks, what="tasks", cap=MAX_PARALLEL_TASKS)
    if err:
        return err
    n = len(tasks)
    if payload.get("run_in_background"):
        return await _parallel_background(ctx, payload, tasks, n)
    return await _parallel_foreground(ctx, payload, tasks, n)


async def _parallel_foreground(ctx, payload: dict, tasks, n: int) -> str:
    """并发跑 N 个独立子，按任务序聚合 bounded envelope。并发上限 = settings max_threads(<=0 不限)。"""
    from ...tools import load_agents_config
    cap = load_agents_config().get("max_threads") or 0
    sem = asyncio.Semaphore(cap if cap and cap > 0 else n)

    async def _one(i: int, t: dict) -> str:
        agent_type = _normalize_agent_type(t.get("type", "general"))
        description = t.get("description") or f"parallel task {i}/{n}"
        task_context = t.get("context") or {"mode": "fresh"}
        try:
            task_context_mode = _context_mode(task_context)
            task_prompt = _project_prompt(str(t["prompt"]), task_context)
        except ValueError as e:
            return f"Error: {e}"
        async with sem:
            try:
                envelope = await ctx.spawn.run_fresh(
                    agent_type, task_prompt, description=description,
                    timeout_ms=t.get("timeout_ms") or payload.get("timeout_ms"),
                    context_mode=task_context_mode, isolation=t.get("isolation"),
                    parallel=True)
            except Exception as e:  # noqa: BLE001
                envelope = f"Sub-agent error: {e}"
        return f"## Task {i}/{n} [{agent_type}] {description}\n{envelope}"

    sections = await asyncio.gather(*[_one(i, t) for i, t in enumerate(tasks, 1)])
    return "\n\n".join(sections)


async def _parallel_background(ctx, payload: dict, tasks, n: int) -> str:
    """派 N 个独立 detached run，立即返回 group id。all-or-nothing 预校验所有 task（不留孤儿子）。"""
    prepared: list[tuple] = []
    for i, t in enumerate(tasks, 1):
        agent_type = _normalize_agent_type(t.get("type", "general"))
        description = t.get("description") or f"parallel task {i}/{n}"
        task_context = t.get("context") or {"mode": "fresh"}
        try:
            task_context_mode = _context_mode(task_context)
            task_prompt = _project_prompt(str(t["prompt"]), task_context)
        except ValueError as e:
            return f"Error: tasks[{i}]: {e}"
        prepared.append((i, agent_type, description, task_prompt, task_context_mode,
                         t.get("isolation"), _bg_timeout(agent_type, t, payload)))
    gid = ctx.spawn.new_group()
    run_ids: list[str] = []
    for (i, agent_type, description, task_prompt,
         task_context_mode, isolation, bg_timeout) in prepared:
        run_id = await ctx.spawn.run_background(
            agent_type, task_prompt, group_id=gid, description=description,
            inject_summary=True, result_summary=f"parallel task {i}/{n}: {description}",
            timeout_ms=bg_timeout, context_mode=task_context_mode, isolation=isolation)
        run_ids.append(run_id)
    listed = "\n".join(f"  - {rid}" for rid in run_ids)
    return (f"Started background parallel group {gid} with {n} task(s):\n{listed}\n"
            f"Each reports completion later (summary auto-injected); do not poll or duplicate "
            f"their work. Cancel the whole group: run_cancel {gid}")
