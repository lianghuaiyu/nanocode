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
from .verify import parse_verdict, validate_schema

MAX_CHAIN_STEPS = 10
MAX_PARALLEL_TASKS = 8
MAX_ACCEPT_ROUNDS = 5          # acceptance-gate 验证轮数硬上限（成本兜底）
MAX_FANOUT_WORKERS = 8         # 动态 fanout worker 数硬上限（= MAX_PARALLEL_TASKS）
PREVIOUS_PLACEHOLDER = "{previous}"
OUTPUT_PLACEHOLDER = "{output}"
_OUTPUT_DISPLAY_CAP = 4000     # 返回模型的「验证后 output」展示截断（全文在 child run_record）


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
    if payload.get("accept") is not None:
        return await _accept(ctx, payload)
    if payload.get("plan_fanout") is not None:
        return await _plan_fanout(ctx, payload)
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
        if ctx.spawn.is_aborted():
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


# ─── acceptance-gate（reviewer-loop + output_schema；docs/26 §0.6 策略库）────────────

def _cap(text: str, limit: int = _OUTPUT_DISPLAY_CAP) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…(truncated; full output in run record)"


def _member_spec(spec: dict, *, default_type: str):
    """从 worker/reviewer/planner 子 spec 解析 (agent_type, projected_prompt, context_mode, timeout_ms,
    isolation, description)；prompt 已按 context 投影。坏 context → ValueError。"""
    agent_type = _normalize_agent_type(spec.get("type", default_type))
    context = spec.get("context") or {"mode": "fresh"}
    context_mode = _context_mode(context)
    prompt = _project_prompt(str(spec["prompt"]), context)
    return agent_type, prompt, context_mode, spec.get("timeout_ms"), spec.get("isolation"), spec.get("description")


async def _accept(ctx, payload: dict) -> str:
    """生产→验证→带反馈 retry。验证器:output_schema(确定性) 先过,再 reviewer(LLM 裁决)。
    worker/reviewer 经 run_step 取**原始 text**(供 schema 解析 / {output} 喂裁决)。≤max_rounds。"""
    spec = payload.get("accept")
    if not isinstance(spec, dict):
        return "Error: 'accept' must be an object {worker, reviewer?, output_schema?, max_rounds?}."
    worker = spec.get("worker")
    if not isinstance(worker, dict) or not str(worker.get("prompt") or "").strip():
        return "Error: accept.worker must be an object with a non-empty 'prompt'."
    reviewer = spec.get("reviewer")
    output_schema = spec.get("output_schema")
    if reviewer is None and output_schema is None:
        return "Error: accept requires at least one verifier: 'reviewer' and/or 'output_schema'."
    if reviewer is not None and (not isinstance(reviewer, dict) or not str(reviewer.get("prompt") or "").strip()):
        return "Error: accept.reviewer must be an object with a non-empty 'prompt' (containing {output})."
    if output_schema is not None and not isinstance(output_schema, dict):
        return "Error: accept.output_schema must be a schema object."
    try:
        max_rounds = int(spec.get("max_rounds", 3))
    except (TypeError, ValueError):
        max_rounds = 3
    max_rounds = max(1, min(max_rounds, MAX_ACCEPT_ROUNDS))

    try:
        w_type, w_base, w_mode, w_timeout, _w_iso, w_desc = _member_spec(worker, default_type="general")
    except ValueError as e:
        return f"Error: accept.worker: {e}"

    sections: list[str] = []
    feedback = ""
    output = ""
    accepted = False
    for r in range(1, max_rounds + 1):
        wp = w_base
        if feedback:
            wp = (f"{w_base}\n\n<reviewer-feedback round {r - 1}>\n{feedback}\n"
                  f"</reviewer-feedback>\nRevise to address the feedback.")
        try:
            output = await ctx.spawn.run_step(
                w_type, wp, description=w_desc or f"accept worker r{r}",
                timeout_ms=w_timeout or payload.get("timeout_ms"), context_mode=w_mode)
        except Exception as e:  # noqa: BLE001
            return f"Sub-agent error (accept worker round {r}): {e}"

        fb_parts: list[str] = []
        ok = True
        if output_schema is not None:
            schema_errs = validate_schema(output, output_schema)
            if schema_errs:
                ok = False
                fb_parts.append("schema errors: " + "; ".join(schema_errs))
        if ok and reviewer is not None:
            try:
                rv_type, _rp, rv_mode, rv_timeout, _rv_iso, rv_desc = _member_spec(
                    {**reviewer, "prompt": str(reviewer["prompt"]).replace(OUTPUT_PLACEHOLDER, output)},
                    default_type="explore")
            except ValueError as e:
                return f"Error: accept.reviewer: {e}"
            try:
                verdict_text = await ctx.spawn.run_step(
                    rv_type, _rp, description=rv_desc or f"accept reviewer r{r}",
                    timeout_ms=rv_timeout or payload.get("timeout_ms"), context_mode=rv_mode)
            except Exception as e:  # noqa: BLE001
                return f"Sub-agent error (accept reviewer round {r}): {e}"
            rv_accept, rv_feedback = parse_verdict(verdict_text)
            if not rv_accept:
                ok = False
                fb_parts.append(f"reviewer: {rv_feedback or '(no feedback)'}")
        accepted = ok
        if accepted:
            sections.append(f"## Round {r}/{max_rounds} — ACCEPTED")
            break
        feedback = "; ".join(fb_parts)
        sections.append(f"## Round {r}/{max_rounds} — rejected\n{feedback or '(no feedback)'}")

    head = (f"# Acceptance gate: {'ACCEPTED' if accepted else f'NOT accepted after {max_rounds} round(s)'} "
            f"({len(sections)} round(s) run)")
    return f"{head}\n\n" + "\n\n".join(sections) + f"\n\n## Verified output\n{_cap(output)}"


# ─── plan-then-fanout（动态分解；docs/26 §0.6 策略库）──────────────────────────────

async def _plan_fanout(ctx, payload: dict) -> str:
    """planner agent 输出 JSON 子任务列表 → fan out workers（复用 parallel 聚合）。"""
    spec = payload.get("plan_fanout")
    if not isinstance(spec, dict):
        return "Error: 'plan_fanout' must be an object {planner, worker_type?, max_workers?}."
    planner = spec.get("planner")
    if not isinstance(planner, dict) or not str(planner.get("prompt") or "").strip():
        return "Error: plan_fanout.planner must be an object with a non-empty 'prompt'."
    worker_default = _normalize_agent_type(spec.get("worker_type", "coder"))
    try:
        max_workers = int(spec.get("max_workers", MAX_FANOUT_WORKERS))
    except (TypeError, ValueError):
        max_workers = MAX_FANOUT_WORKERS
    max_workers = max(1, min(max_workers, MAX_FANOUT_WORKERS))

    try:
        p_type, p_prompt, p_mode, p_timeout, _p_iso, p_desc = _member_spec(planner, default_type="plan")
    except ValueError as e:
        return f"Error: plan_fanout.planner: {e}"
    try:
        plan_text = await ctx.spawn.run_step(
            p_type, p_prompt, description=p_desc or "fanout planner",
            timeout_ms=p_timeout or payload.get("timeout_ms"), context_mode=p_mode)
    except Exception as e:  # noqa: BLE001
        return f"Sub-agent error (planner): {e}"

    subtasks = _parse_subtasks(plan_text)
    if isinstance(subtasks, str):
        return subtasks                          # 错误串（坏 JSON / 形状）
    n_total = len(subtasks)
    if n_total > max_workers:
        ctx.events.notice(
            f"plan_fanout: planner produced {n_total} subtasks; capping to max_workers={max_workers}.",
            level="warn")
        subtasks = subtasks[:max_workers]
    n = len(subtasks)

    from ...tools import load_agents_config
    cap = load_agents_config().get("max_threads") or 0
    sem = asyncio.Semaphore(cap if cap and cap > 0 else n)

    async def _one(i: int, st: dict) -> str:
        agent_type = _normalize_agent_type(st.get("type") or worker_default)
        description = st.get("description") or f"fanout worker {i}/{n}"
        st_context = st.get("context") or {"mode": "fresh"}
        try:
            st_mode = _context_mode(st_context)
            st_prompt = _project_prompt(str(st["prompt"]), st_context)
        except ValueError as e:
            return f"## Worker {i}/{n} [{agent_type}] {description}\nError: {e}"
        async with sem:
            try:
                envelope = await ctx.spawn.run_fresh(
                    agent_type, st_prompt, description=description,
                    timeout_ms=st.get("timeout_ms") or payload.get("timeout_ms"),
                    context_mode=st_mode, isolation=st.get("isolation"), parallel=True)
            except Exception as e:  # noqa: BLE001
                envelope = f"Sub-agent error: {e}"
        return f"## Worker {i}/{n} [{agent_type}] {description}\n{envelope}"

    results = await asyncio.gather(*[_one(i, st) for i, st in enumerate(subtasks, 1)])
    plan = "\n".join(
        f"  {i}. [{_normalize_agent_type(st.get('type') or worker_default)}] "
        f"{st.get('description') or (str(st.get('prompt') or '')[:80])}"
        for i, st in enumerate(subtasks, 1))
    head = f"# Plan-then-fanout: planner decomposed into {n} worker(s)"
    return f"{head}\n\n## Plan\n{plan}\n\n" + "\n\n".join(results)


def _parse_subtasks(plan_text: str):
    """planner 原始 text → 子任务 list（接受裸数组或 {\"subtasks\":[...]}）。
    返回 list（已校验每项 {prompt}）或错误串。"""
    from .verify import _extract_json
    try:
        data = _extract_json(plan_text)
    except Exception:
        return f"Error: planner output is not valid JSON; expected an array of {{description,prompt,type?}}:\n{plan_text}"
    if isinstance(data, dict) and isinstance(data.get("subtasks"), list):
        data = data["subtasks"]
    if not isinstance(data, list) or not data:
        return "Error: planner must output a non-empty JSON array of {description,prompt,type?} subtasks."
    bad = [i for i, s in enumerate(data, 1)
           if not isinstance(s, dict) or not str(s.get("prompt") or "").strip()]
    if bad:
        return f"Error: planner subtasks {bad} must be objects with a non-empty 'prompt'."
    return data
