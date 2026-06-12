"""runtime/spawn.py — SubAgentRunner：子 agent 构造 + 产物/结果落盘（docs/15 §11/§13.8）。

把 engine.py 的子 agent 机器搬迁到 runtime 层（host-driven：方法首参 `host` = 发起的 Agent,
提供 collaborators）。本文件是 Phase 6 的基础切片：构造（build_sub_agent）+ 产物/结果 writers +
token 折叠。执行编排（_execute_agent_tool / foreground·background run）后续切片续搬。

§11.1 不变量（搬迁保持不变）：
- child session 隔离：artifact_id 非 main 的子 agent 注入独立 child 写者租约（SessionLease）;
- `session_id == _tree_session_id` 共享 trajectory_id（独立 child sid 由 _tree_session_id 承载,
  session_id 仍 parent-keyed,故 trajectory_id 不分叉）;
- 后台子 agent：auto-deny 危险确认 + 新空集 confirmed_paths（确认不回流父）;
- host-derived 文件事实由 sub_agent 自身观测（不信任模型）。

_auto_deny_confirm 定义在此并由 engine re-export（tests `from nanocode.agent.engine import
_auto_deny_confirm` + `sub.confirm_fn is _auto_deny_confirm` 的身份断言依赖同一对象）。
"""

from __future__ import annotations

import asyncio
import time

from ..session import v2 as _session_v2
from ..subagents import get_sub_agent_config


async def _auto_deny_confirm(_command: str) -> bool:
    """后台子 agent 的 confirm_fn：无 TTY 等价拒绝（auto-deny-but-continue）。"""
    return False


class SubAgentRunner:
    """子 agent 构造 + 落盘机器（无状态;每 Agent 持一个,方法经 host 注入 collaborators）。"""

    # ─── provider / child id ──────────────────────────────────────────────────
    def current_provider(self, host) -> str:
        return "openai" if host.use_openai else "anthropic"

    def child_session_id(self, host, agent_id: str) -> str:
        """子 agent 的 child session id（docs/14 §6b）。父 sid 作前缀,保证跨父唯一。"""
        return f"{host.session_id}.{agent_id}"

    # ─── 子 agent 构造（集中权限继承）────────────────────────────────────────
    def build_sub_agent(self, host, *, system_prompt, tools, agent_type, session_id=None,
                        background=False, max_turns=None, model=None,
                        artifact_id=None, agent_source=None):
        """构造子 agent：集中权限继承（子继承父 permission_mode、共享 confirm_fn/_confirmed_paths/
        session_id/task_manager;is_sub_agent 工具表强制剔除 agent;前台传有界 max_turns;可选 per-agent
        model 覆盖）。background=True：confirm_fn=_auto_deny_confirm 恒拒 + 新空集 confirmed_paths。

        artifact_id 非 main：注入 child 写者租约（open_or_create:fresh→建 header / resume→打开已存在），
        child sid 由 child_session_id 派生（session_id/artifacts/trajectory 仍 parent-keyed,trajectory_id 不分叉）。
        """
        from ..agent.engine import Agent  # lazy：避免 engine↔spawn 循环 import
        safe_tools = [t for t in tools if t.get("name") != "agent"]
        confirm_fn = _auto_deny_confirm if background else host.confirm_fn
        confirmed_paths = set() if background else host._confirmed_paths
        allowed_tool_names = {t["name"] for t in safe_tools}
        sub = Agent(
            model=model or host.model,
            api_base=str(host._openai_client.base_url) if host.use_openai and host._openai_client else None,
            custom_system_prompt=system_prompt,
            custom_tools=safe_tools,
            is_sub_agent=True,
            permission_mode=host.permission_mode,
            confirm_fn=confirm_fn,
            confirmed_paths=confirmed_paths,
            session_id=session_id or host.session_id,
            task_manager=host.task_manager,
            trajectory_enabled=host.trajectory_enabled,
            trajectory_level=host.trajectory_level,
            max_turns=max_turns,
            artifact_id=artifact_id,
            allowed_tool_names=allowed_tool_names,
            depth=host.depth + 1,
            agent_type=agent_type,
            agent_source=agent_source,
        )
        if artifact_id and artifact_id != "main":
            from ..session.lease import SessionLease
            sub._tree_session_id = host.child_session_id(artifact_id)
            sub._child_parent_session = {"sessionId": host.session_id,
                                         "entryId": host._subagent_spawn_leaf.get(artifact_id),
                                         "taskId": artifact_id, "agentId": artifact_id}
            sub._session_mgr = SessionLease.open_or_create(
                sub._tree_session_id, parent_session=sub._child_parent_session).manager
        return sub

    # ─── token 折叠 ───────────────────────────────────────────────────────────
    def fold_subagent_tokens(self, host, sub_agent) -> None:
        """把子 agent 已花费的 token 折叠进父（成功/超时/错误都折,否则 runaway 成本黑洞）。"""
        try:
            host.total_input_tokens += getattr(sub_agent, "total_input_tokens", 0) or 0
            host.total_output_tokens += getattr(sub_agent, "total_output_tokens", 0) or 0
        except Exception:
            pass

    # ─── 产物 / 结果落盘（parent-keyed artifacts）──────────────────────────────
    def persist_agent_messages(self, host, agent_id: str, sub_agent) -> None:
        """持久化子 agent messages（parent-keyed artifacts,back-compat）+ close child 写锁。"""
        try:
            msgs = sub_agent._dump_messages()
            _session_v2.write_agent_messages(host.session_id, agent_id, msgs)
        except Exception:
            pass
        try:
            if sub_agent._session_mgr is not None:
                sub_agent._session_mgr.close()
        except Exception:
            pass

    def write_agent_spawn_artifacts(self, host, *, agent_id, agent_type, description,
                                    prompt, model, background) -> None:
        """子 agent 创建时落 prompt.txt + meta.json(status=running) + 记 spawn 父 leaf。失败不影响主流程。"""
        try:
            host._subagent_spawn_leaf[agent_id] = (host._session_mgr.get_leaf()
                                                   if host._session_mgr is not None else None)
        except Exception:
            host._subagent_spawn_leaf[agent_id] = None
        try:
            _session_v2.write_agent_prompt(host.session_id, agent_id, prompt or "")
        except Exception:
            pass
        try:
            _session_v2.write_agent_meta(host.session_id, agent_id, {
                "id": agent_id,
                "type": agent_type,
                "description": description,
                "model": model,
                "provider": host._current_provider(),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "background": background,
                "parent_session_id": host.session_id,
                "status": "running",
            })
        except Exception:
            pass

    def finalize_agent_meta(self, host, agent_id: str, status: str) -> None:
        """子 agent 终态补 status + ended_at（合并已有 meta.json）。失败不影响主流程。"""
        try:
            meta = _session_v2.read_agent_meta(host.session_id, agent_id) or {"id": agent_id}
            meta["status"] = status
            meta["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _session_v2.write_agent_meta(host.session_id, agent_id, meta)
        except Exception:
            pass

    def write_agent_result(self, host, agent_id: str, text: str) -> "str | None":
        """子 agent 最终文本 → <agent_dir>/result.md,返回路径（失败 None）。"""
        try:
            return _session_v2.write_agent_result(host.session_id, agent_id, text or "")
        except Exception:
            return None

    def write_subagent_result(self, host, task_id: str, text: str) -> "str | None":
        """子 agent 完整输出 → task_dir/result.md,返回路径（失败 None）。"""
        try:
            d = _session_v2.task_dir(host.session_id, task_id)
            p = d / "result.md"
            p.write_text(text or "", encoding="utf-8")
            return str(p)
        except Exception:
            return None

    def write_terminal_result(self, host, agent_id: str, sub_agent, reason: str) -> "str | None":
        """终态（超时/错误）写 result.md：有 partial 输出就写它,否则写 reason。"""
        partial = host._subagent_captured_text(sub_agent)
        return host._write_agent_result(agent_id, partial or reason)

    # ─── 前台 run 原语 + 终态信封（搬迁自 engine,host-driven）──────────────────
    async def await_subagent_run(self, sub_agent, prompt: str,
                                 timeout_ms: "int | None") -> "tuple[str, object]":
        """可靠地 await 一次子 agent run_once + 可选 wall-clock 超时。返回 (kind, payload):
        'ok'→result dict / 'timeout'→None / 'error'→Exception。

        关键（§7 风险#1）：Agent.chat() 吞 CancelledError（优雅 abort）,故 asyncio.wait_for 在超时 cancel
        后内层吞掉取消、"正常返回"——wait_for 会回值而非抛 TimeoutError。这里用 asyncio.wait 的 pending
        集合可靠判定超时,并用 _aborted 兜底。外层取消先 cancel 内层再上抛,避免任务泄漏。
        """
        inner = asyncio.ensure_future(sub_agent.run_once(prompt))
        try:
            if timeout_ms is not None and timeout_ms > 0:
                done, pending = await asyncio.wait({inner}, timeout=timeout_ms / 1000.0)
                timed_out = inner in pending
            else:
                await asyncio.wait({inner})
                timed_out = False
        except asyncio.CancelledError:
            inner.cancel()
            try:
                await inner
            except BaseException:
                pass
            raise

        if timed_out or getattr(sub_agent, "_aborted", False):
            if not inner.done():
                inner.cancel()
            try:
                await inner
            except BaseException:
                pass
            return "timeout", None
        try:
            return "ok", inner.result()
        except asyncio.CancelledError:
            return "timeout", None
        except Exception as e:  # noqa: BLE001 - 归一为 error，不外泄崩溃父循环
            return "error", e

    async def run_foreground_subagent(self, host, sub_agent, prompt: str,
                                      timeout_ms: "int | None", record_id: "str | None") -> "tuple[str, str | dict]":
        """前台子 agent 执行：施加 wall-clock 超时,永不让异常逃逸。返回 (kind, payload)。"""
        kind, payload = await host._await_subagent_run(sub_agent, prompt, timeout_ms)
        if kind == "timeout":
            if record_id is not None:
                try:
                    host.task_manager.update_subagent(record_id, status="timed_out")
                except Exception:
                    pass
            return "timeout", f"[sub-agent timed out after {timeout_ms} ms]"
        if kind == "error":
            return "error", f"Sub-agent error: {payload}"
        return "ok", payload  # type: ignore[return-value]

    def finalize_foreground_terminal(self, host, sub_agent, record_id: str,
                                     kind: str, payload, timeout_ms: "int | None") -> str:
        """前台 timeout/error 终态共用：折叠 token + 落 partial result.md + 回传带宿主派生
        files_modified 的最小信封（而非裸 '[timed out]' 字符串）。"""
        host._fold_subagent_tokens(sub_agent)
        partial = host._subagent_captured_text(sub_agent)
        reason = (f"[sub-agent timed out after {timeout_ms} ms]" if kind == "timeout"
                  else str(payload))
        result_path = host._write_agent_result(record_id, partial or reason)
        if result_path:
            try:
                host.task_manager.update_subagent(record_id, last_result_path=result_path)
            except Exception:
                pass
        agent_result = host._build_agent_result(
            sub_agent, partial or reason, {"input": 0, "output": 0}, result_path)
        agent_result["summary"] = reason + (
            " — partial transcript persisted" if partial else "")
        return host._render_agent_result_envelope(agent_result, "")

    def finalize_foreground_result(self, host, sub_agent, result: dict,
                                   result_path: "str | None", record_id: "str | None") -> str:
        """前台/skill-fork 成功路径共用：装配 AgentResult → 渲染有界信封 → 回填 last_result_path。"""
        text = result.get("text") or ""
        agent_result = host._build_agent_result(
            sub_agent, text, result.get("tokens") or {}, result_path)
        if record_id is not None and result_path:
            try:
                host.task_manager.update_subagent(record_id, last_result_path=result_path)
            except Exception:
                pass
        return host._render_agent_result_envelope(agent_result, text)

    # ─── agent 工具主入口（fresh / resume / background 分派,搬迁自 engine）─────────
    async def execute_agent_tool(self, host, inp: dict) -> str:
        """`agent` 工具的派发：类型归一 → depth backstop → background / resume / fresh 三路。
        host-driven 搬迁自 engine._execute_agent_tool（行为逐字一致）。"""
        agent_type = inp.get("type", "general")
        from ..subagents.config import _discover_custom_agents, RESERVED_AGENT_TYPES
        if agent_type in ("general", "coder"):
            agent_type = "coder"
        elif agent_type in ("explore", "plan"):
            pass
        elif agent_type in _discover_custom_agents() and agent_type not in RESERVED_AGENT_TYPES:
            pass  # 已发现的自定义类型：保留
        else:
            agent_type = "coder"  # 真正未知（含保留名）→ general 语义
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        resume_id = inp.get("resume")
        tool_timeout_ms = inp.get("timeout_ms")
        from ..tools import load_agents_config
        fleet_cfg = load_agents_config()

        # P4 max_depth backstop（所有 spawn 路径）。
        if host._depth_cap_exceeded():
            return (f"Error: max sub-agent depth ({fleet_cfg.get('max_depth')}) reached; "
                    f"cannot spawn a sub-agent at depth {host.depth + 1}.")

        # ── run_in_background: detached subagent ──
        if inp.get("run_in_background"):
            if resume_id:
                return "Error: run_in_background cannot be combined with resume."
            max_threads = host._max_threads()
            if max_threads > 0 and host._running_background_subagent_count() >= max_threads:
                return (f"Error: max concurrent sub-agents ({max_threads}) reached; try again later.")
            bg_cfg = get_sub_agent_config(agent_type)
            bg_timeout = tool_timeout_ms
            if bg_timeout is None:
                bg_timeout = bg_cfg.get("timeout_ms")
            if bg_timeout is None:
                bg_timeout = fleet_cfg.get("background_timeout_ms")
            task_id = await host._spawn_background_subagent(
                agent_type=agent_type, description=description, prompt=prompt, timeout_ms=bg_timeout)
            return (f"Started background sub-agent task {task_id}. It will report completion later. "
                    f"Use task_output with task_id={task_id} to inspect progress.")

        # ── resume path ──
        if resume_id:
            rec = host.task_manager.get_subagent(resume_id)
            if not rec:
                return f"Error: sub-agent '{resume_id}' not found (unknown id)."
            if rec.type in RESERVED_AGENT_TYPES:
                return (f"Error: sub-agent '{resume_id}' is a reserved internal agent "
                        f"and cannot be resumed via the agent tool.")
            if rec.status == "running":
                return (f"Error: sub-agent '{resume_id}' is still running; cannot resume an in-flight "
                        f"sub-agent. Wait for it to finish (use task_output to check progress).")
            if rec.provider and rec.provider != host._current_provider():
                return (f"Error: provider mismatch — sub-agent '{resume_id}' was created with "
                        f"provider '{rec.provider}' but current provider is '{host._current_provider()}'. "
                        f"Cannot resume across providers.")
            config = get_sub_agent_config(rec.type)
            current_eff_model = config.get("model") or host.model
            if rec.model and rec.model != current_eff_model:
                return (f"Error: model mismatch — sub-agent '{resume_id}' was created with "
                        f"model '{rec.model}' but its current effective model is '{current_eff_model}'. "
                        f"Cannot resume with a different model.")
            eff_timeout = host._foreground_timeout(tool_timeout_ms, config, fleet_cfg)
            max_turns = host._bounded_sub_agent_max_turns(config.get("max_turns"))
            host._sink.sub_agent_start(rec.type, description)
            host.task_manager.update_subagent(resume_id, status="running")
            host._write_agent_spawn_artifacts(
                agent_id=resume_id, agent_type=rec.type, description=description,
                prompt=prompt, model=rec.model or current_eff_model, background=False)
            sub_agent = None
            try:
                sub_agent = host._build_sub_agent(
                    system_prompt=config["system_prompt"], tools=config["tools"],
                    agent_type=rec.type, max_turns=max_turns,
                    model=rec.model or current_eff_model, artifact_id=resume_id,
                    agent_source=config.get("source"))
                kind, payload = await host._run_foreground_subagent(
                    sub_agent, prompt, eff_timeout, resume_id)
            except asyncio.CancelledError:
                host.task_manager.update_subagent(resume_id, status="cancelled")
                if sub_agent is not None:
                    host._persist_agent_messages(resume_id, sub_agent)
                host._finalize_agent_meta(resume_id, "cancelled")
                host._sink.sub_agent_end(rec.type, description)
                raise
            except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
                host.task_manager.update_subagent(resume_id, status="failed")
                if sub_agent is not None:
                    host._persist_agent_messages(resume_id, sub_agent)
                host._finalize_agent_meta(resume_id, "failed")
                host._sink.sub_agent_end(rec.type, description)
                return f"Sub-agent error: {e}"
            if kind != "ok":
                if kind == "error":
                    host.task_manager.update_subagent(resume_id, status="failed")
                host._persist_agent_messages(resume_id, sub_agent)
                host._finalize_agent_meta(
                    resume_id, "timed_out" if kind == "timeout" else "failed")
                host._sink.sub_agent_end(rec.type, description)
                return host._finalize_foreground_terminal(
                    sub_agent, resume_id, kind, payload, eff_timeout)
            result = payload  # type: ignore[assignment]
            host.total_input_tokens += result["tokens"]["input"]
            host.total_output_tokens += result["tokens"]["output"]
            host.task_manager.update_subagent(resume_id, status="completed")
            host._persist_agent_messages(resume_id, sub_agent)
            result_path = host._write_agent_result(resume_id, result["text"] or "")
            host._finalize_agent_meta(resume_id, "completed")
            host._sink.sub_agent_end(rec.type, description)
            return host._finalize_foreground_result(sub_agent, result, result_path, resume_id)

        # ── fresh path ──
        config = get_sub_agent_config(agent_type)
        eff_timeout = host._foreground_timeout(tool_timeout_ms, config, fleet_cfg)
        max_turns = host._bounded_sub_agent_max_turns(config.get("max_turns"))
        eff_model = config.get("model") or host.model
        host._sink.sub_agent_start(agent_type, description)
        rec = host.task_manager.create_subagent(
            type=agent_type, description=description,
            model=eff_model, provider=host._current_provider())
        host.task_manager.update_subagent(rec.id, status="running")
        host._write_agent_spawn_artifacts(
            agent_id=rec.id, agent_type=agent_type, description=description,
            prompt=prompt, model=eff_model, background=False)
        sub_agent = None
        try:
            sub_agent = host._build_sub_agent(
                system_prompt=config["system_prompt"], tools=config["tools"],
                agent_type=agent_type, max_turns=max_turns, model=eff_model,
                artifact_id=rec.id, agent_source=config.get("source"))
            kind, payload = await host._run_foreground_subagent(
                sub_agent, prompt, eff_timeout, rec.id)
        except asyncio.CancelledError:
            host.task_manager.update_subagent(rec.id, status="cancelled")
            if sub_agent is not None:
                host._persist_agent_messages(rec.id, sub_agent)
            host._finalize_agent_meta(rec.id, "cancelled")
            host._sink.sub_agent_end(agent_type, description)
            raise
        except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
            host.task_manager.update_subagent(rec.id, status="failed")
            if sub_agent is not None:
                host._persist_agent_messages(rec.id, sub_agent)
            host._finalize_agent_meta(rec.id, "failed")
            host._sink.sub_agent_end(agent_type, description)
            return f"Sub-agent error: {e}"
        if kind != "ok":
            if kind == "error":
                host.task_manager.update_subagent(rec.id, status="failed")
            host._persist_agent_messages(rec.id, sub_agent)
            host._finalize_agent_meta(
                rec.id, "timed_out" if kind == "timeout" else "failed")
            host._sink.sub_agent_end(agent_type, description)
            return host._finalize_foreground_terminal(
                sub_agent, rec.id, kind, payload, eff_timeout)
        result = payload  # type: ignore[assignment]
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        host.task_manager.update_subagent(rec.id, status="completed")
        host._persist_agent_messages(rec.id, sub_agent)
        result_path = host._write_agent_result(rec.id, result["text"] or "")
        host._finalize_agent_meta(rec.id, "completed")
        host._sink.sub_agent_end(agent_type, description)
        return host._finalize_foreground_result(sub_agent, result, result_path, rec.id)

    # ─── 后台 detached 子 agent（auto-deny-but-continue,搬迁自 engine）────────────
    async def spawn_background_subagent(self, host, *, agent_type: str, description: str,
                                        prompt: str, timeout_ms: "int | None" = None) -> str:
        """注册 subagent + task（双向链）+ detached 协程,立即返回 task_id。"""
        eff_model = get_sub_agent_config(agent_type).get("model") or host.model
        sub_rec = host.task_manager.create_subagent(
            type=agent_type, description=description,
            model=eff_model, provider=host._current_provider())
        host.task_manager.update_subagent(sub_rec.id, status="running")
        task_rec = host.task_manager.create_task("subagent", description, owner_agent_id=sub_rec.id)
        host.task_manager.update_subagent(sub_rec.id, task_id=task_rec.id)
        # docs/14 §6b：记 spawn 时父 leaf,供完成回注 pin 到 spawn 分支（而非完成时 live leaf）。
        try:
            host.task_manager.update_task(
                task_rec.id, spawn_entry_id=(host._session_mgr.get_leaf() if host._session_mgr else None))
        except Exception:
            pass
        host._write_agent_spawn_artifacts(agent_id=sub_rec.id, agent_type=agent_type, description=description,
            prompt=prompt, model=eff_model, background=True)
        host._sink.sub_agent_start(agent_type, description)
        task = asyncio.create_task(host._run_background_subagent(agent_id=sub_rec.id, task_id=task_rec.id, agent_type=agent_type,
            description=description, prompt=prompt, timeout_ms=timeout_ms))
        task._nanocode_task_id = task_rec.id
        host._background_tasks.add(task)
        task.add_done_callback(host._background_tasks.discard)
        return task_rec.id

    async def run_background_subagent(self, host, *, agent_id: str, task_id: str, agent_type: str,
                                      description: str, prompt: str, timeout_ms: "int | None") -> None:
        """detached 协程：构造 background 子 agent,跑 run_once,落终态 + 持久化。"""
        sub_agent = None
        try:
            config = get_sub_agent_config(agent_type)
            sub_agent = host._build_sub_agent(system_prompt=config["system_prompt"], tools=config["tools"],
                agent_type=agent_type, background=True,
                max_turns=host._bounded_sub_agent_max_turns(config.get("max_turns")),
                model=config.get("model"), artifact_id=agent_id, agent_source=config.get("source"))
            kind, payload = await host._await_subagent_run(sub_agent, prompt, timeout_ms)
        except asyncio.CancelledError:
            host.task_manager.update_task(task_id, status="cancelled",
                                          result_summary="(cancelled by task_stop)")
            host.task_manager.update_subagent(agent_id, status="cancelled")
            if sub_agent is not None:
                host._persist_agent_messages(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "cancelled")
            host._sink.sub_agent_end(agent_type, description)
            raise
        except Exception as e:  # noqa: BLE001 — 构造/启动期异常也须落终态,detached 任务不能悬挂 running
            host.task_manager.update_task(task_id, status="failed", error=str(e),
                                          result_summary=f"(sub-agent error: {e})")
            host.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                host._persist_agent_messages(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "failed")
            host._sink.sub_agent_end(agent_type, description)
            return

        if kind == "timeout":
            host._fold_subagent_tokens(sub_agent)
            rp = host._write_terminal_result(agent_id, sub_agent, f"(timed out after {timeout_ms}ms)")
            host.task_manager.update_task(task_id, status="timed_out", result_path=rp,
                                          result_summary=f"(timed out after {timeout_ms}ms)")
            host.task_manager.update_subagent(agent_id, status="failed", last_result_path=rp)
            if sub_agent is not None:
                host._persist_agent_messages(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "timed_out")
            host._sink.sub_agent_end(agent_type, description)
            return
        if kind == "error":
            host._fold_subagent_tokens(sub_agent)
            rp = host._write_terminal_result(agent_id, sub_agent, f"(sub-agent error: {payload})")
            host.task_manager.update_task(task_id, status="failed", error=str(payload), result_path=rp,
                                          result_summary=f"(sub-agent error: {payload})")
            host.task_manager.update_subagent(agent_id, status="failed", last_result_path=rp)
            if sub_agent is not None:
                host._persist_agent_messages(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "failed")
            host._sink.sub_agent_end(agent_type, description)
            return

        result = payload  # kind == "ok"
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = host._write_subagent_result(task_id, text)
        agent_result_path = host._write_agent_result(agent_id, text)
        agent_result = host._build_agent_result(sub_agent, text, result["tokens"], result_path)
        host.task_manager.update_task(task_id, status="completed", result_path=result_path,
                                      result_summary=agent_result["summary"])
        host.task_manager.update_subagent(agent_id, status="completed", last_result_path=agent_result_path)
        host._persist_agent_messages(agent_id, sub_agent)
        host._finalize_agent_meta(agent_id, "completed")
        host._sink.sub_agent_end(agent_type, description)
