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

_auto_deny_confirm 定义在此（docs/16 C-1：engine re-export 已删，调用方/测试直接
`from nanocode.runtime.spawn import _auto_deny_confirm`）。
"""

from __future__ import annotations

import asyncio
import json
import time

from ..memory.maintenance import (
    apply_plan,
    build_curator_user_message,
    build_eval_curator_message,
    parse_consolidation_plan,
)
from ..agent.subagent_manager import SubAgentManager
from ..agent.events import SubAgentStarted, SubAgentEnded
from ..session import v2 as _session_v2
from ..agents.permissions import effective_child_tools
from ..agents.result import ResultEnvelope
from ..agents.profile import AgentProfile, IsolationPolicy, PermissionProfile
from ..agents.registry import build_profile


async def _auto_deny_confirm(_command: str) -> bool:
    """后台子 agent 的 confirm_fn：无 TTY 等价拒绝（auto-deny-but-continue）。"""
    return False


def _normalize_agent_type(agent_type: str) -> str:
    """agent 工具的类型归一（单步/chain/parallel 共用）：general/coder 同义归 coder；
    已发现的自定义类型保留；未知/保留名 → coder（general 语义）。"""
    from ..agents.registry import RESERVED_AGENT_TYPES, discover_custom_agents
    if agent_type in ("general", "coder"):
        return "coder"
    if agent_type in ("explore", "plan"):
        return agent_type
    if agent_type in discover_custom_agents() and agent_type not in RESERVED_AGENT_TYPES:
        return agent_type
    return "coder"


def live_agent_profile(host) -> AgentProfile:
    """live Agent 的 profile 视图（docs/16 #7b：child 派生的 parent 侧输入）。

    只投影派生所需面：tools_allow = 父的 call-time allowlist（主 agent 恒 None）、
    permission.mode = 父当前 mode（子不得高于父）、can_spawn = 非子 agent。"""
    return AgentProfile(
        name=host.agent_type or ("subagent" if host.is_sub_agent else "build"),
        mode="subagent" if host.is_sub_agent else "primary",
        model=host.model,
        tools_allow=(set(host._allowed_tool_names) if host._allowed_tool_names is not None else None),
        tools_deny=set(),
        permission=PermissionProfile(mode=host.permission_mode),
        isolation=IsolationPolicy(own_session=True, can_spawn=not host.is_sub_agent),
    )


def child_tools(host, profile: AgentProfile, *, background: bool = False) -> list:
    """父+子 profile 派生的子 agent 有效 ToolDef 列表（docs/16 #7b：
    derive_child_profile/effective_child_tools 上 live spawn 路径）。

    语义：allow 交集（子绝不获得父没有的工具）、deny 并集、永剔 'agent'（子不 spawn 孙）。
    主 agent 父（allow=None、deny=∅）下与 registry.effective_tools(profile) 逐字节等价——
    typed 派生让「子不得超过父」对未来的非主 parent 也结构性成立。"""
    from ..tools import tool_definitions
    names = effective_child_tools(live_agent_profile(host), profile,
                                  {t["name"] for t in tool_definitions}, background=background)
    return [t for t in tool_definitions if t["name"] in names]


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
    def close_child_session(self, host, agent_id: str, sub_agent) -> None:
        """子 agent 运行结束：close child 写者租约。历史唯一权威是 child canonical 树
        （resume 经 child tree 重载），不再落 messages.json 副本（docs/16 C-1）。"""
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
        files_modified 的最小信封（而非裸 '[timed out]' 字符串）。docs/16 #7b：typed
        ResultEnvelope（render 保 ≤4KB 直通/截断+指针 bound 不变）。"""
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
        envelope = ResultEnvelope.build(
            sub_agent, partial or reason, {"input": 0, "output": 0}, result_path,
            status=("timed_out" if kind == "timeout" else "failed"),
            error=(None if kind == "timeout" else str(payload)))
        envelope.summary = reason + (
            " — partial transcript persisted" if partial else "")
        return envelope.render("")

    def finalize_foreground_result(self, host, sub_agent, result: dict,
                                   result_path: "str | None", record_id: "str | None") -> str:
        """前台/skill-fork 成功路径共用：装配 typed ResultEnvelope → 渲染有界信封（≤4KB 直通/
        截断+指针）→ 回填 last_result_path。父 branch 只存信封 + child session id（§11.1）。"""
        text = result.get("text") or ""
        envelope = ResultEnvelope.build(sub_agent, text, result.get("tokens") or {}, result_path)
        if record_id is not None and result_path:
            try:
                host.task_manager.update_subagent(record_id, last_result_path=result_path)
            except Exception:
                pass
        return envelope.render(text)

    # ─── agent 工具主入口（fresh / resume / background 分派,搬迁自 engine）─────────
    async def execute_agent_tool(self, host, inp: dict) -> str:
        """`agent` 工具的派发：类型归一 → depth backstop → background / resume / fresh 三路。
        host-driven 搬迁自 engine._execute_agent_tool（行为逐字一致）。"""
        from ..agents.registry import RESERVED_AGENT_TYPES
        agent_type = _normalize_agent_type(inp.get("type", "general"))
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        resume_id = inp.get("resume")
        tool_timeout_ms = inp.get("timeout_ms")
        from ..tools import load_agents_config
        fleet_cfg = load_agents_config()

        # P4 max_depth backstop（所有 spawn 路径，含 chain/parallel）。
        if host._subagents.depth_cap_exceeded():
            return (f"Error: max sub-agent depth ({fleet_cfg.get('max_depth')}) reached; "
                    f"cannot spawn a sub-agent at depth {host.depth + 1}.")

        # ── chain / parallel fan-in（docs/16 #9：审计过的 host 原语）──
        if inp.get("steps") is not None or inp.get("tasks") is not None:
            if inp.get("steps") is not None and inp.get("tasks") is not None:
                return "Error: 'steps' (chain) and 'tasks' (parallel) cannot be combined."
            if resume_id or inp.get("run_in_background"):
                return "Error: steps/tasks cannot be combined with resume or run_in_background."
            if inp.get("steps") is not None:
                return await self.execute_agent_chain(host, inp, fleet_cfg)
            return await self.execute_agent_parallel(host, inp, fleet_cfg)

        if not (prompt or "").strip() and not resume_id:
            return "Error: 'prompt' is required (or pass steps/tasks for orchestration)."

        # ── run_in_background: detached subagent ──
        if inp.get("run_in_background"):
            if resume_id:
                return "Error: run_in_background cannot be combined with resume."
            max_threads = host._subagents.max_threads()
            if max_threads > 0 and host._subagents.running_background_count() >= max_threads:
                return (f"Error: max concurrent sub-agents ({max_threads}) reached; try again later.")
            bg_timeout = tool_timeout_ms
            if bg_timeout is None:
                bg_timeout = build_profile(agent_type).timeout_ms
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
            profile = build_profile(rec.type)
            current_eff_model = profile.model or host.model
            if rec.model and rec.model != current_eff_model:
                return (f"Error: model mismatch — sub-agent '{resume_id}' was created with "
                        f"model '{rec.model}' but its current effective model is '{current_eff_model}'. "
                        f"Cannot resume with a different model.")
            eff_timeout = SubAgentManager.foreground_timeout(tool_timeout_ms, profile.timeout_ms, fleet_cfg)
            max_turns = host._subagents.bounded_max_turns(profile.max_turns)
            host.emit(SubAgentStarted(agent_type=rec.type, description=description))
            host.task_manager.update_subagent(resume_id, status="running")
            host._write_agent_spawn_artifacts(
                agent_id=resume_id, agent_type=rec.type, description=description,
                prompt=prompt, model=rec.model or current_eff_model, background=False)
            sub_agent = None
            try:
                sub_agent = host._build_sub_agent(
                    system_prompt=profile.prompt, tools=child_tools(host, profile),
                    agent_type=rec.type, max_turns=max_turns,
                    model=rec.model or current_eff_model, artifact_id=resume_id,
                    agent_source=profile.source)
                kind, payload = await host._run_foreground_subagent(
                    sub_agent, prompt, eff_timeout, resume_id)
            except asyncio.CancelledError:
                host.task_manager.update_subagent(resume_id, status="cancelled")
                if sub_agent is not None:
                    host._close_child_session(resume_id, sub_agent)
                host._finalize_agent_meta(resume_id, "cancelled")
                host.emit(SubAgentEnded(agent_type=rec.type, description=description))
                raise
            except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
                host.task_manager.update_subagent(resume_id, status="failed")
                if sub_agent is not None:
                    host._close_child_session(resume_id, sub_agent)
                host._finalize_agent_meta(resume_id, "failed")
                host.emit(SubAgentEnded(agent_type=rec.type, description=description))
                return f"Sub-agent error: {e}"
            if kind != "ok":
                if kind == "error":
                    host.task_manager.update_subagent(resume_id, status="failed")
                host._close_child_session(resume_id, sub_agent)
                host._finalize_agent_meta(
                    resume_id, "timed_out" if kind == "timeout" else "failed")
                host.emit(SubAgentEnded(agent_type=rec.type, description=description))
                return host._finalize_foreground_terminal(
                    sub_agent, resume_id, kind, payload, eff_timeout)
            result = payload  # type: ignore[assignment]
            host.total_input_tokens += result["tokens"]["input"]
            host.total_output_tokens += result["tokens"]["output"]
            host.task_manager.update_subagent(resume_id, status="completed")
            host._close_child_session(resume_id, sub_agent)
            result_path = host._write_agent_result(resume_id, result["text"] or "")
            host._finalize_agent_meta(resume_id, "completed")
            host.emit(SubAgentEnded(agent_type=rec.type, description=description))
            return host._finalize_foreground_result(sub_agent, result, result_path, resume_id)

        # ── fresh path ──
        return await self.run_fresh_subagent(
            host, agent_type=agent_type, description=description, prompt=prompt,
            tool_timeout_ms=tool_timeout_ms, fleet_cfg=fleet_cfg)

    async def run_fresh_subagent(self, host, *, agent_type: str, description: str,
                                 prompt: str, tool_timeout_ms: "int | None",
                                 fleet_cfg: dict) -> str:
        """fresh 前台子 agent 单步原语（execute_agent_tool fresh 路径逐字抽出；
        chain/parallel 复用——每步/每任务都是独立 record + 独立 leased child session +
        bounded ResultEnvelope）。除 CancelledError 外不抛（错误归一为字符串）。"""
        profile = build_profile(agent_type)
        eff_timeout = SubAgentManager.foreground_timeout(tool_timeout_ms, profile.timeout_ms, fleet_cfg)
        max_turns = host._subagents.bounded_max_turns(profile.max_turns)
        eff_model = profile.model or host.model
        host.emit(SubAgentStarted(agent_type=agent_type, description=description))
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
                system_prompt=profile.prompt, tools=child_tools(host, profile),
                agent_type=agent_type, max_turns=max_turns, model=eff_model,
                artifact_id=rec.id, agent_source=profile.source)
            kind, payload = await host._run_foreground_subagent(
                sub_agent, prompt, eff_timeout, rec.id)
        except asyncio.CancelledError:
            host.task_manager.update_subagent(rec.id, status="cancelled")
            if sub_agent is not None:
                host._close_child_session(rec.id, sub_agent)
            host._finalize_agent_meta(rec.id, "cancelled")
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            raise
        except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
            host.task_manager.update_subagent(rec.id, status="failed")
            if sub_agent is not None:
                host._close_child_session(rec.id, sub_agent)
            host._finalize_agent_meta(rec.id, "failed")
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return f"Sub-agent error: {e}"
        if kind != "ok":
            if kind == "error":
                host.task_manager.update_subagent(rec.id, status="failed")
            host._close_child_session(rec.id, sub_agent)
            host._finalize_agent_meta(
                rec.id, "timed_out" if kind == "timeout" else "failed")
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return host._finalize_foreground_terminal(
                sub_agent, rec.id, kind, payload, eff_timeout)
        result = payload  # type: ignore[assignment]
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        host.task_manager.update_subagent(rec.id, status="completed")
        host._close_child_session(rec.id, sub_agent)
        result_path = host._write_agent_result(rec.id, result["text"] or "")
        host._finalize_agent_meta(rec.id, "completed")
        host.emit(SubAgentEnded(agent_type=agent_type, description=description))
        return host._finalize_foreground_result(sub_agent, result, result_path, rec.id)

    # ─── chain / parallel fan-in（docs/16 #9：借 pi {previous} 与 fan-in 的编排点子）──
    #
    # 审计过的 host 原语：每步/每任务都经 run_fresh_subagent（独立 SubAgentRecord、独立
    # leased child session、bounded ResultEnvelope、同一 fail-closed allowlist/确认链）；
    # **不耦合** TeamRuntime（其 team_* entry 保持 non-FOLD/non-leaf，与此无涉）。
    # 上限防御：步数/任务数封顶；parallel 并发受 settings [agents].max_threads 约束。

    MAX_CHAIN_STEPS = 10
    MAX_PARALLEL_TASKS = 8
    PREVIOUS_PLACEHOLDER = "{previous}"

    @staticmethod
    def _validate_orchestration_items(items, *, what: str, cap: int) -> "str | None":
        if not isinstance(items, list) or not items:
            return f"Error: '{what}' must be a non-empty array of {{type?, description?, prompt}} objects."
        if len(items) > cap:
            return f"Error: too many {what} ({len(items)} > {cap})."
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict) or not str(it.get("prompt") or "").strip():
                return f"Error: {what}[{i}] must be an object with a non-empty 'prompt'."
        return None

    async def execute_agent_chain(self, host, inp: dict, fleet_cfg: dict) -> str:
        """chain：按序跑 N 个独立子 agent；步 prompt 里的 {previous} 替换为上一步的
        bounded envelope（pi index.ts:530 的 {previous} 点子）。abort 在步边界生效。"""
        steps = inp.get("steps")
        err = self._validate_orchestration_items(steps, what="steps", cap=self.MAX_CHAIN_STEPS)
        if err:
            return err
        n = len(steps)
        previous = "(no previous step)"
        sections: list[str] = []
        for i, step in enumerate(steps, 1):
            if host._aborted:
                sections.append(f"## Step {i}/{n} — skipped (turn aborted)")
                break
            agent_type = _normalize_agent_type(step.get("type", "general"))
            description = step.get("description") or f"chain step {i}/{n}"
            prompt = str(step["prompt"]).replace(self.PREVIOUS_PLACEHOLDER, previous)
            envelope = await self.run_fresh_subagent(
                host, agent_type=agent_type, description=description, prompt=prompt,
                tool_timeout_ms=step.get("timeout_ms") or inp.get("timeout_ms"),
                fleet_cfg=fleet_cfg)
            previous = envelope
            sections.append(f"## Step {i}/{n} [{agent_type}] {description}\n{envelope}")
        return "\n\n".join(sections)

    async def execute_agent_parallel(self, host, inp: dict, fleet_cfg: dict) -> str:
        """parallel fan-in：并发跑 N 个独立子 agent，按任务序聚合 bounded envelope。
        并发上限 = settings [agents].max_threads（<=0 视为不限）；取消经 gather 传播到
        全部子任务（各自的 CancelledError 处理落终态后再上抛）。"""
        tasks = inp.get("tasks")
        err = self._validate_orchestration_items(tasks, what="tasks", cap=self.MAX_PARALLEL_TASKS)
        if err:
            return err
        n = len(tasks)
        cap = host._subagents.max_threads()
        sem = asyncio.Semaphore(cap if cap and cap > 0 else n)

        async def _one(i: int, t: dict) -> str:
            agent_type = _normalize_agent_type(t.get("type", "general"))
            description = t.get("description") or f"parallel task {i}/{n}"
            async with sem:
                envelope = await self.run_fresh_subagent(
                    host, agent_type=agent_type, description=description,
                    prompt=str(t["prompt"]),
                    tool_timeout_ms=t.get("timeout_ms") or inp.get("timeout_ms"),
                    fleet_cfg=fleet_cfg)
            return f"## Task {i}/{n} [{agent_type}] {description}\n{envelope}"

        sections = await asyncio.gather(*[_one(i, t) for i, t in enumerate(tasks, 1)])
        return "\n\n".join(sections)

    # ─── 后台 detached 子 agent（auto-deny-but-continue,搬迁自 engine）────────────
    async def spawn_background_subagent(self, host, *, agent_type: str, description: str,
                                        prompt: str, timeout_ms: "int | None" = None) -> str:
        """注册 subagent + task（双向链）+ detached 协程,立即返回 task_id。"""
        eff_model = build_profile(agent_type).model or host.model
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
        host.emit(SubAgentStarted(agent_type=agent_type, description=description))
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
            profile = build_profile(agent_type)
            sub_agent = host._build_sub_agent(system_prompt=profile.prompt,
                tools=child_tools(host, profile, background=True),
                agent_type=agent_type, background=True,
                max_turns=host._subagents.bounded_max_turns(profile.max_turns),
                model=profile.model, artifact_id=agent_id, agent_source=profile.source)
            kind, payload = await host._await_subagent_run(sub_agent, prompt, timeout_ms)
        except asyncio.CancelledError:
            host.task_manager.update_task(task_id, status="cancelled",
                                          result_summary="(cancelled by task_stop)")
            host.task_manager.update_subagent(agent_id, status="cancelled")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "cancelled")
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            raise
        except Exception as e:  # noqa: BLE001 — 构造/启动期异常也须落终态,detached 任务不能悬挂 running
            host.task_manager.update_task(task_id, status="failed", error=str(e),
                                          result_summary=f"(sub-agent error: {e})")
            host.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "failed")
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return

        if kind == "timeout":
            host._fold_subagent_tokens(sub_agent)
            rp = host._write_terminal_result(agent_id, sub_agent, f"(timed out after {timeout_ms}ms)")
            host.task_manager.update_task(task_id, status="timed_out", result_path=rp,
                                          result_summary=f"(timed out after {timeout_ms}ms)")
            host.task_manager.update_subagent(agent_id, status="failed", last_result_path=rp)
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "timed_out")
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return
        if kind == "error":
            host._fold_subagent_tokens(sub_agent)
            rp = host._write_terminal_result(agent_id, sub_agent, f"(sub-agent error: {payload})")
            host.task_manager.update_task(task_id, status="failed", error=str(payload), result_path=rp,
                                          result_summary=f"(sub-agent error: {payload})")
            host.task_manager.update_subagent(agent_id, status="failed", last_result_path=rp)
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "failed")
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return

        result = payload  # kind == "ok"
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = host._write_subagent_result(task_id, text)
        agent_result_path = host._write_agent_result(agent_id, text)
        envelope = ResultEnvelope.build(sub_agent, text, result["tokens"], result_path)
        host.task_manager.update_task(task_id, status="completed", result_path=result_path,
                                      result_summary=envelope.summary)
        host.task_manager.update_subagent(agent_id, status="completed", last_result_path=agent_result_path)
        host._close_child_session(agent_id, sub_agent)
        host._finalize_agent_meta(agent_id, "completed")
        host.emit(SubAgentEnded(agent_type=agent_type, description=description))

    # ─── memory curator spawns（搬迁自 engine,host-driven;curator 是 subagent,§13.8）──────────
    # ─── Memory consolidation (Auto-Dream) ────────────────────

    async def spawn_memory_consolidate(self, host) -> str:
        """触发记忆巩固：curator 子 agent 出 JSON 提案 → 宿主 parse+apply。

        无记忆短路：build_curator_user_message() 返回 "No memory files..." 时不建
        task/subagent，直接返回提示。否则注册 subagent(memory-curator) + task
        (memory_consolidate, owner=agent_id) + detached _run_memory_consolidate，
        立即返回 task_id 提示（同 _spawn_background_subagent 的双向链 + 后台登记）。
        """
        user_message = build_curator_user_message()
        if user_message.startswith("No memory files"):
            return "No memories to consolidate."
        if host._subagents.background_cap_reached():
            return (f"Error: max concurrent sub-agents ({host._subagents.max_threads()}) reached; "
                    f"memory consolidation not started — try again later.")

        description = "memory consolidation"
        sub_rec = host.task_manager.create_subagent(
            type=host._MEMORY_CURATOR_TYPE, description=description,
            model=host.model, provider=host._current_provider(),
        )
        host.task_manager.update_subagent(sub_rec.id, status="running")
        task_rec = host.task_manager.create_task(
            "memory_consolidate", description, owner_agent_id=sub_rec.id)
        host.task_manager.update_subagent(sub_rec.id, task_id=task_rec.id)
        host._write_agent_spawn_artifacts(
            agent_id=sub_rec.id, agent_type=host._MEMORY_CURATOR_TYPE,
            description=description, prompt=user_message, model=host.model,
            background=True)
        host.emit(SubAgentStarted(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
        task = asyncio.create_task(host._run_memory_consolidate(
            agent_id=sub_rec.id, task_id=task_rec.id, user_message=user_message))
        task._nanocode_task_id = task_rec.id
        host._background_tasks.add(task)
        task.add_done_callback(host._background_tasks.discard)
        return (f"Started memory consolidation task {task_rec.id}. It will report completion later. "
                f"Use task_output with task_id={task_rec.id} to inspect the proposal + result.")

    async def run_memory_consolidate(self, host, *, agent_id: str, task_id: str,
                                      user_message: str, timeout_ms: int | None = None) -> None:
        """detached 协程：判断型(curator)+确定性(Python apply)解耦。

        **不复用** _run_background_subagent（后者把子文本当最终 result；巩固需 parse+apply
        后处理）。**绕开** _execute_agent_tool（其 type 归一会把 memory-curator 改成 coder
        拿全工具）——直接 build_profile(memory-curator)（恒无工具）+ _build_sub_agent
        (background=True)。四态对称：cancel/timeout/error 写终态；成功则 token 累加 + 持久化
        messages + 写 result.md，再 parse(坏JSON→completed "no changes")+apply→summary_line。
        """
        sub_agent = None
        description = "memory consolidation"
        try:
            profile = build_profile(host._MEMORY_CURATOR_TYPE)
            sub_agent = host._build_sub_agent(
                system_prompt=profile.prompt,
                tools=child_tools(host, profile, background=True),
                agent_type=host._MEMORY_CURATOR_TYPE,
                background=True,
                max_turns=host._subagents.bounded_max_turns(profile.max_turns),
                artifact_id=agent_id,
            )
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    sub_agent.run_once(user_message), timeout=timeout_ms / 1000.0)
            else:
                result = await sub_agent.run_once(user_message)
        except asyncio.CancelledError:
            host.task_manager.update_task(
                task_id, status="cancelled",
                result_summary="(cancelled by task_stop)")
            host.task_manager.update_subagent(agent_id, status="cancelled")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "cancelled")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            raise
        except asyncio.TimeoutError:
            host.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            host.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "timed_out")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            return
        except Exception as e:
            host.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(curator error: {e})")
            host.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "failed")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            return

        # curator 成功产出 JSON 提案：token 累加 + 持久化 + 写 result.md
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = host._write_subagent_result(task_id, text)
        host._write_agent_result(agent_id, text)
        host.task_manager.update_subagent(agent_id, status="completed")
        host._close_child_session(agent_id, sub_agent)
        host._finalize_agent_meta(agent_id, "completed")

        # 确定性 parse+apply（宿主 Python，可回滚）。坏 JSON 不让 task failed，标 completed。
        try:
            plan = parse_consolidation_plan(text)
        except Exception:
            host.task_manager.update_task(
                task_id, status="completed", result_path=result_path,
                result_summary="Consolidation: no changes (unparseable plan)")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            return

        apply_result = apply_plan(plan)
        host.task_manager.update_task(
            task_id, status="completed", result_path=result_path,
            result_summary=apply_result.summary_line())
        host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))

    # ─── Memory eval candidate generation (EVAL-mode curator) ──

    async def spawn_memory_eval(self, host) -> str:
        """触发 eval 候选生成：EVAL-mode curator 子 agent 出候选 JSON →
        宿主逐条 add_pending（非法跳过）。无记忆短路。"""
        user_message = build_eval_curator_message()
        if user_message.startswith("No memory files"):
            return "No memories to generate eval candidates from."
        if host._subagents.background_cap_reached():
            return (f"Error: max concurrent sub-agents ({host._subagents.max_threads()}) reached; "
                    f"memory eval not started — try again later.")
        # eval 候选 provenance 的 source.session_id 必须指向真实存在的 session，
        # 否则 add_pending 校验会拒掉全部候选。REPL 命令不走 chat()，在此显式落盘。
        host._persist_state()

        description = "memory eval generation"
        sub_rec = host.task_manager.create_subagent(
            type=host._MEMORY_EVAL_CURATOR_TYPE, description=description,
            model=host.model, provider=host._current_provider(),
        )
        host.task_manager.update_subagent(sub_rec.id, status="running")
        task_rec = host.task_manager.create_task(
            "memory_eval", description, owner_agent_id=sub_rec.id)
        host.task_manager.update_subagent(sub_rec.id, task_id=task_rec.id)
        host._write_agent_spawn_artifacts(
            agent_id=sub_rec.id, agent_type=host._MEMORY_EVAL_CURATOR_TYPE,
            description=description, prompt=user_message, model=host.model,
            background=True)
        host.emit(SubAgentStarted(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
        task = asyncio.create_task(host._run_memory_eval(
            agent_id=sub_rec.id, task_id=task_rec.id, user_message=user_message))
        task._nanocode_task_id = task_rec.id
        host._background_tasks.add(task)
        task.add_done_callback(host._background_tasks.discard)
        return (f"Started memory eval generation task {task_rec.id}. It will report completion later. "
                f"Use task_output with task_id={task_rec.id} to inspect generated candidates.")

    async def run_memory_eval(self, host, *, agent_id: str, task_id: str,
                               user_message: str, timeout_ms: int | None = None) -> None:
        """detached 协程：curator 出候选 JSON → 宿主逐条 eval_store.add_pending。

        宿主强制 source.session_id = host.session_id（不信任 curator）。校验失败的
        候选计入 skipped，不让 task failed。坏 JSON → completed 0 candidates。"""
        from ..memory import eval_store
        sub_agent = None
        description = "memory eval generation"
        try:
            profile = build_profile(host._MEMORY_EVAL_CURATOR_TYPE)
            sub_agent = host._build_sub_agent(
                system_prompt=profile.prompt,
                tools=child_tools(host, profile, background=True),
                agent_type=host._MEMORY_EVAL_CURATOR_TYPE,
                background=True,
                max_turns=host._subagents.bounded_max_turns(profile.max_turns),
                artifact_id=agent_id,
            )
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    sub_agent.run_once(user_message), timeout=timeout_ms / 1000.0)
            else:
                result = await sub_agent.run_once(user_message)
        except asyncio.CancelledError:
            host.task_manager.update_task(
                task_id, status="cancelled",
                result_summary="(cancelled by task_stop)")
            host.task_manager.update_subagent(agent_id, status="cancelled")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "cancelled")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
            raise
        except asyncio.TimeoutError:
            host.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            host.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "timed_out")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
            return
        except Exception as e:
            host.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(eval curator error: {e})")
            host.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host._finalize_agent_meta(agent_id, "failed")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
            return

        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = host._write_subagent_result(task_id, text)
        host._write_agent_result(agent_id, text)
        host.task_manager.update_subagent(agent_id, status="completed")
        host._close_child_session(agent_id, sub_agent)
        host._finalize_agent_meta(agent_id, "completed")

        # 确定性后处理：解析候选并逐条 add_pending（坏 JSON / 缺 candidates → 0）。
        added = 0
        skipped = 0
        try:
            from ..memory.maintenance import extract_json_object
            data = json.loads(extract_json_object(text))
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
        except Exception:
            host.task_manager.update_task(
                task_id, status="completed", result_path=result_path,
                result_summary="Generated 0 pending eval candidates (unparseable output)")
            host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
            return

        for item in candidates:
            if not isinstance(item, dict):
                skipped += 1
                continue
            source = dict(item.get("source") or {})
            source["session_id"] = host.session_id  # 宿主统一填 provenance
            cand = eval_store.MemoryEvalCandidate(
                question=item.get("question", ""),
                answer=item.get("answer", ""),
                source=source,
                evidence=list(item.get("evidence") or []),
                category=item.get("category", "general"),
                confidence=float(item.get("confidence") or 0.0),
            )
            try:
                eval_store.add_pending(cand)
                added += 1
            except Exception:
                skipped += 1

        summary = f"Generated {added} pending eval candidate(s)"
        if skipped:
            summary += f" ({skipped} skipped)"
        host.task_manager.update_task(
            task_id, status="completed", result_path=result_path,
            result_summary=summary)
        host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))

    # ─── Memory optimization (EvolveMem, host-only) ───────────

    async def spawn_memory_optimize(self, host) -> str:
        """触发记忆检索配置优化：prune confirmed evals → 阈值门控 →
        simplemem.optimize → 原子落 evolve_config.json。纯宿主计算（无 curator）。

        与 consolidate/eval 不同：**不**注册 subagent（optimize 非判断型任务），
        仅建 task（kind=memory_optimize, owner=None）+ detached _run_memory_optimize。
        也**不**短路：即便 backend 不可用也建 task，让 task 报告 unavailable，
        这样 REPL 用户能 task_output 看到有意义的诊断结果。
        """
        description = "memory optimization"
        task_rec = host.task_manager.create_task(
            "memory_optimize", description, owner_agent_id=None)
        task = asyncio.create_task(host._run_memory_optimize(task_id=task_rec.id))
        task._nanocode_task_id = task_rec.id
        host._background_tasks.add(task)
        task.add_done_callback(host._background_tasks.discard)
        return (f"Started memory optimization task {task_rec.id}. It will report completion later. "
                f"Use task_output with task_id={task_rec.id} to inspect the result.")

    async def run_memory_optimize(self, host, *, task_id: str, timeout_ms: int | None = None) -> None:
        """detached 协程：纯宿主优化计算。四态对称（cancel/timeout 仅为对称保留）。

        ① backend 非 simplemem（duck-type：name != "simplemem" 或无 _system）→
           completed + unavailable 提示（有意义的诊断结果，非短路）。
        ② prune_orphaned_evals(eval/confirmed)：源记忆已巩固归档/合并的孤儿 confirmed
           被清掉，避免 EvolveMem 在 stale 信号上优化。
        ③ 阈值门控：confirmed_dev_questions() 在 prune 之后 < 阈值 → completed + skipped。
        ④ 够数 → 拿 finalized SimpleMem 实例（backend._system，即 SimpleMemSystem，
           直接暴露 llm_client/embedding_model/get_all_memories，_resolve_backend 兼容）
           → simplemem.optimize → Config → save_evolve_config(asdict)（保留 .bak）。
        ⑤ optimize 抛异常 → failed + error，且**不调 save** → 旧 config 原样保留。
        """
        from ..memory import eval_store
        from ..memory.maintenance import (
            prune_orphaned_evals, save_evolve_config, _simplemem_dir,
            evolve_min_confirmed, evolve_max_rounds,
        )
        from dataclasses import asdict as _asdict

        backend = host._memory_backend
        try:
            # ① backend duck-type 判定（不 import SimpleMemBackend，避免顶层耦合）
            mem = getattr(backend, "_system", None)
            if getattr(backend, "name", "") != "simplemem" or mem is None:
                host.task_manager.update_task(
                    task_id, status="completed",
                    result_summary="memory_optimize unavailable: backend is not simplemem")
                return

            # ② prune 孤儿 confirmed evals（源记忆已被巩固归档/合并）
            confirmed_dir = _simplemem_dir() / "eval" / "confirmed"
            pruned = prune_orphaned_evals(eval_dir=confirmed_dir)

            # ③ 阈值门控（prune 之后）
            dev = eval_store.confirmed_dev_questions()
            threshold = evolve_min_confirmed()
            if len(dev) < threshold:
                host.task_manager.update_task(
                    task_id, status="completed",
                    result_summary=(f"memory_optimize skipped: confirmed {len(dev)} "
                                    f"< threshold {threshold} (pruned {pruned})"))
                return

            # ④ 跑 optimize（测试 monkeypatch simplemem.optimize；绝不真跑 EvolveMem）
            from .._vendor import simplemem
            max_rounds = evolve_max_rounds()
            config = simplemem.optimize(mem, dev, max_rounds=max_rounds)

            # ⑤ 原子落 config（save_evolve_config 已 .bak 备份 + tmp 替换）
            path = save_evolve_config(_asdict(config))
            rounds = getattr(config, "evolution_rounds", "?")
            host.task_manager.update_task(
                task_id, status="completed",
                result_summary=(f"memory_optimize: evolved config saved "
                                f"({len(dev)} dev questions, pruned {pruned}, "
                                f"rounds {rounds}) -> {path}"))
        except asyncio.CancelledError:
            host.task_manager.update_task(
                task_id, status="cancelled",
                result_summary="(cancelled by task_stop)")
            raise
        except asyncio.TimeoutError:
            host.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            return
        except Exception as e:
            host.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(optimize error: {e})")
            return

