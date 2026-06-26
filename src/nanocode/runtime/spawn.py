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
import os
from contextlib import contextmanager
from dataclasses import replace

from ..memory.maintenance import (
    apply_plan,
    build_curator_user_message,
    parse_consolidation_plan,
)
from ..agent.subagent_manager import SubAgentManager
from ..agent.events import NoticeRaised, SubAgentStarted, SubAgentEnded
from ..session import v2 as _session_v2
from ..agents.permissions import effective_child_tools
from ..agents.result import ResultEnvelope
from ..agents.profile import AgentProfile, IsolationPolicy, PermissionProfile
from ..agents.registry import build_profile
from ..session import tree as _tree
from ..session.manager import SessionManager
from ..subagents import run_record
from ..subagents.worktree import create_worktree, diff_summary, should_isolate


@contextmanager
def _push_cwd(cwd: str | None):
    if not cwd:
        yield
        return
    old = os.getcwd()
    if old == cwd:
        yield
        return
    os.chdir(cwd)
    try:
        yield
    finally:
        os.chdir(old)


def _child_cwd(host, *, cwd: str | None = None) -> str | None:
    """子 agent 工作目录派生（docs/23 Step 7-S3，单一来源）。

    worktree cwd 覆盖优先 → host 的 RuntimeServices.cwd → host._session_mgr._cwd() → None。
    与搬迁前 build_sub_agent / apply_child_worktree / create_failed_run_record 三处内联派生
    逐字一致。"""
    if cwd is not None:
        return cwd
    services = getattr(host, "_runtime_services", None)
    if services is not None:
        return services.cwd
    return host._session_mgr._cwd() if host._session_mgr is not None else None


def _child_runtime_services(host, *, cwd: str | None = None):
    """子 agent 的 RuntimeServices bundle，从 host bundle 派生（docs/23 Step 7-S3）。

    host 无 bundle（白盒/测试 agent）→ 返回 None，子 agent 保持 bundle-less，在进程 cwd 跑
    （与今天一致）。唯一被子 agent 读取的字段是 `.cwd`（await_subagent_run / engine._effective_cwd），
    其余字段（memory_service/context_sources/extension_host）在子 agent 路径上从不被读。"""
    services = getattr(host, "_runtime_services", None)
    if services is None:
        return None
    return replace(services, cwd=_child_cwd(host, cwd=cwd))


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


def _model_snapshot(host, model: str) -> dict:
    return {"provider": host._current_provider(), "modelId": model}


def _context_mode(context) -> str:
    if context is None:
        return "fresh"
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    mode = context.get("mode", "fresh")
    if mode not in {"fresh", "fork_summary", "branch_projection"}:
        raise ValueError(f"unsupported context.mode: {mode}")
    return mode


def _project_prompt(prompt: str, context) -> str:
    mode = _context_mode(context)
    if mode == "fresh":
        return prompt
    if mode == "fork_summary":
        summary = (context or {}).get("summary")
        if not summary:
            raise ValueError("context.mode=fork_summary requires summary")
        from_entry = (context or {}).get("fromEntryId")
        return (
            f"{prompt}\n\n"
            f"<parent-branch-summary fromEntryId=\"{from_entry or ''}\">\n"
            f"{summary}\n"
            f"</parent-branch-summary>"
        )
    include = ", ".join(str(x) for x in ((context or {}).get("include") or []))
    from_entry = (context or {}).get("fromEntryId")
    return (
        f"{prompt}\n\n"
        f"<parent-branch-projection fromEntryId=\"{from_entry or ''}\" include=\"{include}\" />"
    )


def _last_message_entry_id(agent, role: str) -> str | None:
    mgr = getattr(agent, "_session_mgr", None)
    if mgr is None:
        return None
    out = None
    for e in mgr.entries():
        if e.type == _tree.MESSAGE and (e.data.get("message") or {}).get("role") == role:
            out = e.id
    return out


def _first_new_message_entry_id(agent, before_ids: set[str], role: str) -> str | None:
    mgr = getattr(agent, "_session_mgr", None)
    if mgr is None:
        return None
    for e in mgr.entries():
        if e.id in before_ids:
            continue
        if e.type == _tree.MESSAGE and (e.data.get("message") or {}).get("role") == role:
            return e.id
    return None


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
    from ..tools import REGISTRY
    tool_definitions = REGISTRY.schemas()
    names = effective_child_tools(live_agent_profile(host), profile,
                                  {t["name"] for t in tool_definitions}, background=background)
    return [t for t in tool_definitions if t["name"] in names]


class SubAgentRunner:
    """子 agent 构造 + 落盘机器（无状态;每 Agent 持一个,方法经 host 注入 collaborators）。"""

    # ─── provider / child id ──────────────────────────────────────────────────
    def current_provider(self, host) -> str:
        return "openai" if host.use_openai else "anthropic"

    def child_session_id(self, host, agent_id: str) -> str:
        """Return the child-session id for a sub-agent run.

        New sub-agent identities are already child session ids. Callers must not
        pass legacy ``agent-001`` style artifact keys here.
        """
        return agent_id

    def new_child_session_id(self) -> str:
        return _tree.new_id("sess")

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
            # docs/19 §8：子 agent 继承父 sandbox profile（收窄不放宽——同 profile 即不放宽，
            # narrow_policy_for_context 再按 is_subagent/background/hook 上下文收紧）。
            sandbox_profile=getattr(host, "_sandbox_profile", "default"),
        )
        if artifact_id and artifact_id != "main":
            from ..session.lease import SessionLease
            sub._tree_session_id = host.child_session_id(artifact_id)
            sub._child_parent_session = {"sessionId": host.session_id,
                                         "entryId": host._subagent_spawn_leaf.get(artifact_id),
                                         "taskId": artifact_id, "agentId": artifact_id}
            cwd = _child_cwd(host)
            sub._session_lease = SessionLease.open_or_create(
                sub._tree_session_id, parent_session=sub._child_parent_session, cwd=cwd)
            sub._session_mgr = sub._session_lease.manager
            # docs/23 Step 7-S3：子 agent 携带一致的 RuntimeServices bundle（host 无 bundle → None,
            # 保持 bundle-less）。production 非 worktree 子 agent 自此持 cwd=host cwd 的 bundle,与旧
            # 「未设 → await_subagent_run 在进程 cwd 跑」逐字等价（host bundle cwd == 进程 cwd）。
            sub._runtime_services = _child_runtime_services(host)
        return sub

    def materialize_child_session(self, sub_agent) -> None:
        mgr = getattr(sub_agent, "_session_mgr", None)
        if mgr is not None and not SessionManager.exists(mgr.session_id):
            mgr.rewrite_file()

    def apply_child_worktree(self, host, sub_agent, worktree_path: str | None) -> None:
        if not worktree_path:
            return
        # docs/23 Step 7-S3：worktree cwd 覆盖经同一派生口（host 无 bundle → None,不设）。
        services = _child_runtime_services(host, cwd=worktree_path)
        if services is not None:
            sub_agent._runtime_services = services

    def attach_run_record_projector(self, sub_agent, child_session_id: str) -> None:
        """Project child UI events into the child-owned run sidecar.

        This observer is intentionally a sidecar projection only. The canonical
        child transcript remains ``session.jsonl`` via AgentSession.record_event.
        """
        def _project(event) -> None:
            kind = getattr(event, "kind", None)
            if kind == "tool_call_requested":
                run_record.record_tool_started(
                    child_session_id,
                    tool=event.tool,
                    tool_use_id=event.tool_use_id,
                    tool_input=event.input,
                )
            elif kind == "tool_result_observed":
                run_record.record_tool_finished(
                    child_session_id,
                    tool=event.tool,
                    tool_use_id=event.tool_use_id,
                    chars=event.chars,
                    result=event.result,
                )
            elif kind == "tool_result_completed":
                run_record.record_tool_finished(
                    child_session_id,
                    tool=event.tool,
                    tool_use_id=event.tool_use_id,
                    chars=len(event.content or ""),
                    result=event.content,
                    is_error=event.is_error,
                    latency_ms=event.latency_ms,
                )
            elif kind == "turn_completed":
                run_record.record_turn_completed(
                    child_session_id,
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    turns=event.turns,
                    status="completed",
                )
            elif kind == "turn_aborted":
                run_record.record_turn_completed(
                    child_session_id,
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    turns=event.turns,
                    status="aborted",
                )
            elif kind == "compaction_requested":
                run_record.record_compaction_requested(
                    child_session_id,
                    reason=event.reason,
                )

        sub_agent._event_subscribers.append(_project)

    def begin_run_record(self, host, *, sub_agent, agent_id: str, agent_type: str,
                         description: str, prompt: str, model: str, background: bool,
                         context_mode: str, isolation: str, worktree_path: str | None,
                         status: str = "running", inject_summary: bool = False) -> str:
        self.materialize_child_session(sub_agent)
        child_session_id = sub_agent._tree_session_id
        run_record.create_run_record(
            child_session_id=child_session_id,
            parent_session_id=host.session_id,
            spawn_entry_id=host._subagent_spawn_leaf.get(agent_id),
            tool_call_id=None,
            agent_type=agent_type,
            description=description,
            background=background,
            context_mode=context_mode,
            isolation=isolation,
            worktree_path=worktree_path,
            model=_model_snapshot(host, model),
            prompt=prompt,
            status=status,
            inject_summary=inject_summary,
        )
        self.attach_run_record_projector(sub_agent, child_session_id)
        run_record.append_event(child_session_id, "session_ready")
        if status == "running":
            run_record.append_event(child_session_id, "started")
        elif status == "queued":
            run_record.append_event(child_session_id, "queued")
        return child_session_id

    def create_failed_run_record(self, host, *, child_session_id: str, agent_type: str,
                                 description: str, prompt: str, model: str, background: bool,
                                 context_mode: str, isolation: str,
                                 worktree_path: str | None, error: str) -> str:
        from ..session.lease import SessionLease
        parent_session = {
            "sessionId": host.session_id,
            "entryId": host._subagent_spawn_leaf.get(child_session_id),
            "taskId": child_session_id,
            "agentId": child_session_id,
        }
        cwd = _child_cwd(host)
        lease = SessionLease.open_or_create(child_session_id, parent_session=parent_session, cwd=cwd)
        try:
            if not SessionManager.exists(child_session_id):
                lease.manager.rewrite_file()
        finally:
            lease.close()
        run_record.create_run_record(
            child_session_id=child_session_id,
            parent_session_id=host.session_id,
            spawn_entry_id=host._subagent_spawn_leaf.get(child_session_id),
            tool_call_id=None,
            agent_type=agent_type,
            description=description,
            background=background,
            context_mode=context_mode,
            isolation=isolation,
            worktree_path=worktree_path,
            model=_model_snapshot(host, model),
            prompt=prompt,
            status="running",
        )
        run_record.complete_run(
            child_session_id,
            status="failed",
            result=f"Sub-agent error: {error}",
            prompt_entry_id=None,
            result_entry_id=None,
            error=error,
        )
        return child_session_id

    def finish_run_record(self, *, sub_agent, status: str, result_text: str,
                          tokens: dict | None = None, error: str | None = None,
                          result_summary: str | None = None) -> str | None:
        child_session_id = getattr(sub_agent, "_tree_session_id", None)
        if not child_session_id:
            return None
        prompt_entry_id = (
            getattr(sub_agent, "_nanocode_last_prompt_entry_id", None)
            or _last_message_entry_id(sub_agent, "user")
        )
        result_entry_id = _last_message_entry_id(sub_agent, "assistant")
        snapshot = run_record.complete_run(
            child_session_id,
            status=status,
            result=result_text or "",
            prompt_entry_id=prompt_entry_id,
            result_entry_id=result_entry_id,
            tokens=tokens,
            error=error,
            result_summary=result_summary,
        )
        return snapshot.get("resultPath")

    # ─── token 折叠 ───────────────────────────────────────────────────────────
    def fold_subagent_tokens(self, host, sub_agent) -> None:
        """把子 agent 已花费的 token 折叠进父（成功/超时/错误都折,否则 runaway 成本黑洞）。"""
        try:
            host.total_input_tokens += getattr(sub_agent, "total_input_tokens", 0) or 0
            host.total_output_tokens += getattr(sub_agent, "total_output_tokens", 0) or 0
        except Exception:
            pass

    # ─── child session lifecycle + host task result helpers ────────────────────
    def close_child_session(self, host, agent_id: str, sub_agent) -> None:
        """子 agent 运行结束：close child 写者租约。历史唯一权威是 child canonical 树
        （resume 经 child tree 重载），不再落 messages.json 副本（docs/16 C-1）。"""
        try:
            lease = getattr(sub_agent, "_session_lease", None)
            if lease is not None:
                lease.close()
                sub_agent._session_lease = None
            elif sub_agent._session_mgr is not None:
                sub_agent._session_mgr.close()
            sub_agent._session_mgr = None
        except Exception:
            pass

    def record_subagent_spawn_leaf(self, host, child_session_id: str) -> None:
        """Remember the parent leaf that spawned this child session."""
        try:
            host._subagent_spawn_leaf[child_session_id] = (
                host._session_mgr.get_leaf() if host._session_mgr is not None else None)
        except Exception:
            host._subagent_spawn_leaf[child_session_id] = None

    def write_host_task_result(self, host, task_id: str, text: str) -> "str | None":
        """Internal host job output -> tasks/<task_id>/result.md."""
        try:
            d = _session_v2.task_dir(host.session_id, task_id)
            p = d / "result.md"
            p.write_text(text or "", encoding="utf-8")
            return str(p)
        except Exception:
            return None

    # ─── 前台 run 原语 + 终态信封（搬迁自 engine,host-driven）──────────────────
    async def await_subagent_run(self, sub_agent, prompt: str,
                                 timeout_ms: "int | None") -> "tuple[str, object]":
        """可靠地 await 一次子 agent run_once + 可选 wall-clock 超时。返回 (kind, payload):
        'ok'→result dict / 'timeout'→None / 'error'→Exception。

        关键（§7 风险#1）：Agent.chat() 吞 CancelledError（优雅 abort）,故 asyncio.wait_for 在超时 cancel
        后内层吞掉取消、"正常返回"——wait_for 会回值而非抛 TimeoutError。这里用 asyncio.wait 的 pending
        集合可靠判定超时,并用 _aborted 兜底。外层取消先 cancel 内层再上抛,避免任务泄漏。
        """
        async def _run_once_in_child_cwd():
            services = getattr(sub_agent, "_runtime_services", None)
            cwd = services.cwd if services is not None else None
            mgr = getattr(sub_agent, "_session_mgr", None)
            before_ids = {e.id for e in mgr.entries()} if mgr is not None else set()
            with _push_cwd(cwd):
                result = await sub_agent.run_once(prompt)
            sub_agent._nanocode_last_prompt_entry_id = _first_new_message_entry_id(
                sub_agent, before_ids, "user")
            return result

        inner = asyncio.ensure_future(_run_once_in_child_cwd())
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
        result_path = None
        if record_id:
            try:
                result_path = run_record.read_status(record_id).get("resultPath")
            except Exception:
                result_path = None
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
        steer_id = inp.get("steer")
        tool_timeout_ms = inp.get("timeout_ms")
        context = inp.get("context") or {"mode": "fresh"}
        try:
            context_mode = _context_mode(context)
            prompt = _project_prompt(prompt, context)
        except ValueError as e:
            return f"Error: {e}"
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
            if resume_id or steer_id or inp.get("run_in_background"):
                return "Error: steps/tasks cannot be combined with resume, steer, or run_in_background."
            if inp.get("steps") is not None:
                return await self.execute_agent_chain(host, inp, fleet_cfg)
            return await self.execute_agent_parallel(host, inp, fleet_cfg)

        if steer_id:
            if resume_id:
                return "Error: steer cannot be combined with resume."
            if not (prompt or "").strip():
                return "Error: 'prompt' is required when using steer."
            try:
                return host.run_send(
                    steer_id, prompt,
                    delivery=inp.get("delivery") or "steer",
                )
            except Exception as e:
                return f"Error: {e}"

        if not (prompt or "").strip() and not resume_id:
            return "Error: 'prompt' is required (or pass steps/tasks for orchestration)."

        # ── run_in_background: detached subagent ──
        if inp.get("run_in_background"):
            if resume_id:
                return "Error: run_in_background cannot be combined with resume."
            bg_timeout = tool_timeout_ms
            if bg_timeout is None:
                bg_timeout = build_profile(agent_type).timeout_ms
            if bg_timeout is None:
                bg_timeout = fleet_cfg.get("background_timeout_ms")
            try:
                run_id = await host._spawn_background_subagent(
                    agent_type=agent_type, description=description, prompt=prompt, timeout_ms=bg_timeout,
                    context_mode=context_mode, isolation=inp.get("isolation"))
            except Exception as e:
                return f"Error: {e}"
            return (f"Started background sub-agent run {run_id}. "
                    f"It will report completion later; do not poll for progress or duplicate its work.")

        # ── resume path ──
        if resume_id:
            try:
                run = host._run_runtime.status(resume_id)
            except Exception:
                return f"Error: sub-agent run '{resume_id}' not found."
            if run.agent_type in RESERVED_AGENT_TYPES:
                return (f"Error: sub-agent '{resume_id}' is a reserved internal agent "
                        f"and cannot be resumed via the agent tool.")
            if run.status == "running":
                return (f"Error: sub-agent '{resume_id}' is still running; cannot resume an in-flight "
                        f"sub-agent. Send a steer message if you need to adjust it; otherwise wait for completion.")
            rec_provider = (run.model or {}).get("provider")
            rec_model = (run.model or {}).get("modelId")
            if rec_provider and rec_provider != host._current_provider():
                return (f"Error: provider mismatch — sub-agent '{resume_id}' was created with "
                        f"provider '{rec_provider}' but current provider is '{host._current_provider()}'. "
                        f"Cannot resume across providers.")
            profile = build_profile(run.agent_type)
            current_eff_model = profile.model or host.model
            if rec_model and rec_model != current_eff_model:
                return (f"Error: model mismatch — sub-agent '{resume_id}' was created with "
                        f"model '{rec_model}' but its current effective model is '{current_eff_model}'. "
                        f"Cannot resume with a different model.")
            eff_timeout = SubAgentManager.foreground_timeout(tool_timeout_ms, profile.timeout_ms, fleet_cfg)
            max_turns = host._subagents.bounded_max_turns(profile.max_turns)
            host.emit(SubAgentStarted(agent_type=run.agent_type, description=description))
            sub_agent = None
            try:
                self.record_subagent_spawn_leaf(host, resume_id)
                sub_agent = host._build_sub_agent(
                    system_prompt=profile.prompt, tools=child_tools(host, profile),
                    agent_type=run.agent_type, max_turns=max_turns,
                    model=rec_model or current_eff_model, artifact_id=resume_id,
                    agent_source=profile.source)
                host._spawn.materialize_child_session(sub_agent)
                host._spawn.attach_run_record_projector(sub_agent, resume_id)
                run_record.update_status(
                    resume_id, status="running", startedAt=_tree.now_iso(), endedAt=None,
                    error=None)
                run_record.append_event(resume_id, "started", resume=True)
                from ..subagents.steer import drain_pending_steers
                drain_pending_steers(sub_agent, delivery="steer")
                kind, payload = await host._run_foreground_subagent(
                    sub_agent, prompt, eff_timeout, resume_id)
            except asyncio.CancelledError:
                if sub_agent is not None:
                    host._spawn.finish_run_record(
                        sub_agent=sub_agent, status="cancelled",
                        result_text=host._subagent_captured_text(sub_agent) or "(cancelled)",
                        error="cancelled")
                    host._close_child_session(resume_id, sub_agent)
                host.emit(SubAgentEnded(agent_type=run.agent_type, description=description))
                raise
            except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
                if sub_agent is not None:
                    host._spawn.finish_run_record(
                        sub_agent=sub_agent, status="failed",
                        result_text=host._subagent_captured_text(sub_agent) or f"Sub-agent error: {e}",
                        error=str(e))
                    host._close_child_session(resume_id, sub_agent)
                host.emit(SubAgentEnded(agent_type=run.agent_type, description=description))
                return f"Sub-agent error: {e}"
            if kind != "ok":
                host._spawn.finish_run_record(
                    sub_agent=sub_agent,
                    status="timed_out" if kind == "timeout" else "failed",
                    result_text=host._subagent_captured_text(sub_agent) or str(payload),
                    error=None if kind == "timeout" else str(payload))
                host._close_child_session(resume_id, sub_agent)
                host.emit(SubAgentEnded(agent_type=run.agent_type, description=description))
                return host._finalize_foreground_terminal(
                    sub_agent, resume_id, kind, payload, eff_timeout)
            result = payload  # type: ignore[assignment]
            host.total_input_tokens += result["tokens"]["input"]
            host.total_output_tokens += result["tokens"]["output"]
            result_path = host._spawn.finish_run_record(
                sub_agent=sub_agent, status="completed",
                result_text=result["text"] or "", tokens=result["tokens"])
            host._close_child_session(resume_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=run.agent_type, description=description))
            return host._finalize_foreground_result(sub_agent, result, result_path, resume_id)

        # ── fresh path ──
        try:
            return await self.run_fresh_subagent(
                host, agent_type=agent_type, description=description, prompt=prompt,
                tool_timeout_ms=tool_timeout_ms, fleet_cfg=fleet_cfg,
                context_mode=context_mode, isolation=inp.get("isolation"), parallel=False)
        except Exception as e:
            return f"Sub-agent error: {e}"

    async def run_fresh_subagent(self, host, *, agent_type: str, description: str,
                                 prompt: str, tool_timeout_ms: "int | None",
                                 fleet_cfg: dict, context_mode: str = "fresh",
                                 isolation: str | None = None,
                                 parallel: bool = False) -> str:
        """fresh 前台子 agent 单步原语（execute_agent_tool fresh 路径逐字抽出；
        chain/parallel 复用——每步/每任务都是独立 record + 独立 leased child session +
        bounded ResultEnvelope）。除 CancelledError 外不抛（错误归一为字符串）。"""
        profile = build_profile(agent_type)
        eff_timeout = SubAgentManager.foreground_timeout(tool_timeout_ms, profile.timeout_ms, fleet_cfg)
        max_turns = host._subagents.bounded_max_turns(profile.max_turns)
        eff_model = profile.model or host.model
        child_id = self.new_child_session_id()
        selected_isolation = should_isolate(agent_type=agent_type, parallel=parallel,
                                            requested=isolation)
        worktree_path = None
        if selected_isolation == "worktree":
            cwd = host._effective_cwd() if hasattr(host, "_effective_cwd") else "."
            worktree = create_worktree(cwd, child_id)
            worktree_path = worktree.path
        host.emit(SubAgentStarted(agent_type=agent_type, description=description))
        self.record_subagent_spawn_leaf(host, child_id)
        sub_agent = None
        try:
            sub_agent = host._build_sub_agent(
                system_prompt=profile.prompt, tools=child_tools(host, profile),
                agent_type=agent_type, max_turns=max_turns, model=eff_model,
                artifact_id=child_id, agent_source=profile.source)
            self.apply_child_worktree(host, sub_agent, worktree_path)
            run_id = self.begin_run_record(
                host, sub_agent=sub_agent, agent_id=child_id, agent_type=agent_type,
                description=description, prompt=prompt, model=eff_model, background=False,
                context_mode=context_mode, isolation=selected_isolation,
                worktree_path=worktree_path)
            if worktree_path:
                run_record.append_event(run_id, "worktree_created", worktreePath=worktree_path)
            kind, payload = await host._run_foreground_subagent(
                sub_agent, prompt, eff_timeout, child_id)
        except asyncio.CancelledError:
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="cancelled",
                    result_text=host._subagent_captured_text(sub_agent) or "(cancelled)",
                    error="cancelled")
                host._close_child_session(child_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            raise
        except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="failed",
                    result_text=host._subagent_captured_text(sub_agent) or f"Sub-agent error: {e}",
                    error=str(e))
                host._close_child_session(child_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return f"Sub-agent error: {e}"
        if kind != "ok":
            self.finish_run_record(
                sub_agent=sub_agent,
                status="timed_out" if kind == "timeout" else "failed",
                result_text=host._subagent_captured_text(sub_agent) or str(payload),
                error=None if kind == "timeout" else str(payload))
            host._close_child_session(child_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return host._finalize_foreground_terminal(
                sub_agent, child_id, kind, payload, eff_timeout)
        result = payload  # type: ignore[assignment]
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        result_path = self.finish_run_record(
            sub_agent=sub_agent, status="completed",
            result_text=result["text"] or "", tokens=result["tokens"])
        if worktree_path:
            run_record.append_event(child_id, "worktree_finalized",
                                    diffSummary=diff_summary(worktree_path))
        host._close_child_session(child_id, sub_agent)
        host.emit(SubAgentEnded(agent_type=agent_type, description=description))
        return host._finalize_foreground_result(sub_agent, result, result_path, child_id)

    # ─── chain / parallel fan-in（docs/16 #9：借 pi {previous} 与 fan-in 的编排点子）──
    #
    # 审计过的 host 原语：每步/每任务都经 run_fresh_subagent（独立 child session +
    # run record、bounded ResultEnvelope、同一 fail-closed allowlist/确认链）；
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
            step_context = step.get("context") or {"mode": "fresh"}
            try:
                step_context_mode = _context_mode(step_context)
                prompt = _project_prompt(
                    str(step["prompt"]).replace(self.PREVIOUS_PLACEHOLDER, previous),
                    step_context,
                )
            except ValueError as e:
                return f"Error: {e}"
            try:
                envelope = await self.run_fresh_subagent(
                    host, agent_type=agent_type, description=description, prompt=prompt,
                    tool_timeout_ms=step.get("timeout_ms") or inp.get("timeout_ms"),
                    fleet_cfg=fleet_cfg,
                    context_mode=step_context_mode,
                    isolation=step.get("isolation"),
                    parallel=False)
            except Exception as e:
                envelope = f"Sub-agent error: {e}"
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
            task_context = t.get("context") or {"mode": "fresh"}
            try:
                task_context_mode = _context_mode(task_context)
                task_prompt = _project_prompt(str(t["prompt"]), task_context)
            except ValueError as e:
                return f"Error: {e}"
            async with sem:
                try:
                    envelope = await self.run_fresh_subagent(
                        host, agent_type=agent_type, description=description,
                        prompt=task_prompt,
                        tool_timeout_ms=t.get("timeout_ms") or inp.get("timeout_ms"),
                        fleet_cfg=fleet_cfg,
                        context_mode=task_context_mode,
                        isolation=t.get("isolation"),
                        parallel=True)
                except Exception as e:
                    envelope = f"Sub-agent error: {e}"
            return f"## Task {i}/{n} [{agent_type}] {description}\n{envelope}"

        sections = await asyncio.gather(*[_one(i, t) for i, t in enumerate(tasks, 1)])
        return "\n\n".join(sections)

    # ─── 后台 detached 子 agent（auto-deny-but-continue,搬迁自 engine）────────────

    async def wait_for_background_slot(self, host, run_id: str) -> None:
        while True:
            queue = getattr(host, "_background_run_queue", [])
            max_threads = host._subagents.max_threads()
            at_head = bool(queue) and queue[0] == run_id
            has_slot = max_threads <= 0 or host._subagents.running_background_count() < max_threads
            if at_head and has_slot:
                queue.pop(0)
                run_record.update_status(
                    run_id,
                    status="running",
                    startedAt=_tree.now_iso(),
                    endedAt=None,
                    error=None,
                )
                run_record.append_event(run_id, "started")
                return
            await asyncio.sleep(0.05)

    async def spawn_background_subagent(self, host, *, agent_type: str, description: str,
                                        prompt: str, timeout_ms: "int | None" = None,
                                        context_mode: str = "fresh",
                                        isolation: str | None = None) -> str:
        """注册 child-session run + detached 协程,立即返回 child_session_id。"""
        eff_model = build_profile(agent_type).model or host.model
        child_id = self.new_child_session_id()
        selected_isolation = should_isolate(agent_type=agent_type, parallel=False,
                                            requested=isolation)
        worktree_path = None
        if selected_isolation == "worktree":
            cwd = host._effective_cwd() if hasattr(host, "_effective_cwd") else "."
            worktree = create_worktree(cwd, child_id)
            worktree_path = worktree.path
        self.record_subagent_spawn_leaf(host, child_id)
        host.emit(SubAgentStarted(agent_type=agent_type, description=description))
        profile = build_profile(agent_type)
        try:
            sub_agent = host._build_sub_agent(system_prompt=profile.prompt,
                tools=child_tools(host, profile, background=True),
                agent_type=agent_type, background=True,
                max_turns=host._subagents.bounded_max_turns(profile.max_turns),
                model=profile.model, artifact_id=child_id, agent_source=profile.source)
        except Exception as e:
            self.create_failed_run_record(
                host, child_session_id=child_id, agent_type=agent_type,
                description=description, prompt=prompt, model=eff_model, background=True,
                context_mode=context_mode, isolation=selected_isolation,
                worktree_path=worktree_path, error=str(e))
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return child_id
        self.apply_child_worktree(host, sub_agent, worktree_path)
        run_id = self.begin_run_record(
            host, sub_agent=sub_agent, agent_id=child_id, agent_type=agent_type,
            description=description, prompt=prompt, model=eff_model, background=True,
            context_mode=context_mode, isolation=selected_isolation,
            worktree_path=worktree_path,
            status=(
                "queued"
                if host._subagents.max_threads() > 0
                and host._subagents.running_background_count() >= host._subagents.max_threads()
                else "running"
            ))
        queued = run_record.read_status(run_id)["status"] == "queued"
        if queued:
            host._background_run_queue.append(run_id)
        if worktree_path:
            run_record.append_event(run_id, "worktree_created", worktreePath=worktree_path)
        task = asyncio.create_task(host._run_background_subagent(agent_id=child_id, agent_type=agent_type,
            description=description, prompt=prompt, timeout_ms=timeout_ms, sub_agent=sub_agent,
            worktree_path=worktree_path, queued=queued))
        task._nanocode_run_id = run_id
        host._background_tasks.add(task)
        task.add_done_callback(host._background_tasks.discard)
        return run_id

    async def run_background_subagent(self, host, *, agent_id: str, agent_type: str,
                                      description: str, prompt: str, timeout_ms: "int | None",
                                      sub_agent=None, worktree_path: str | None = None,
                                      queued: bool = False) -> None:
        """detached 协程：构造 background 子 agent,跑 run_once,落终态 + 持久化。"""
        try:
            if queued:
                await self.wait_for_background_slot(host, agent_id)
            kind, payload = await host._await_subagent_run(sub_agent, prompt, timeout_ms)
        except asyncio.CancelledError:
            try:
                host._background_run_queue.remove(agent_id)
            except ValueError:
                pass
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="cancelled",
                    result_text=host._subagent_captured_text(sub_agent) or "(cancelled)",
                    error="cancelled")
                host._close_child_session(agent_id, sub_agent)
            host.emit(NoticeRaised(
                text=f"Background sub-agent run {agent_id} cancelled: {description}",
                level="warn"))
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            raise
        except Exception as e:  # noqa: BLE001 — 构造/启动期异常也须落终态,detached 任务不能悬挂 running
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="failed",
                    result_text=host._subagent_captured_text(sub_agent) or f"(sub-agent error: {e})",
                    error=str(e))
                host._close_child_session(agent_id, sub_agent)
            host.emit(NoticeRaised(
                text=f"Background sub-agent run {agent_id} failed: {description}",
                level="warn"))
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return

        if kind == "timeout":
            host._fold_subagent_tokens(sub_agent)
            text = host._subagent_captured_text(sub_agent) or f"(timed out after {timeout_ms}ms)"
            self.finish_run_record(
                sub_agent=sub_agent, status="timed_out",
                result_text=text, error=None)
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host.emit(NoticeRaised(
                text=f"Background sub-agent run {agent_id} timed out: {description}",
                level="warn"))
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return
        if kind == "error":
            host._fold_subagent_tokens(sub_agent)
            text = host._subagent_captured_text(sub_agent) or f"(sub-agent error: {payload})"
            self.finish_run_record(
                sub_agent=sub_agent, status="failed",
                result_text=text, error=str(payload))
            if sub_agent is not None:
                host._close_child_session(agent_id, sub_agent)
            host.emit(NoticeRaised(
                text=f"Background sub-agent run {agent_id} failed: {description}",
                level="warn"))
            host.emit(SubAgentEnded(agent_type=agent_type, description=description))
            return

        result = payload  # kind == "ok"
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        self.finish_run_record(
            sub_agent=sub_agent, status="completed", result_text=text, tokens=result["tokens"])
        if worktree_path:
            run_record.append_event(agent_id, "worktree_finalized",
                                    diffSummary=diff_summary(worktree_path))
        host._close_child_session(agent_id, sub_agent)
        host.emit(NoticeRaised(
            text=f"Background sub-agent run {agent_id} completed: {description}"))
        host.emit(SubAgentEnded(agent_type=agent_type, description=description))

    # ─── memory curator spawns（搬迁自 engine,host-driven;curator 是 subagent,§13.8）──────────
    # ─── Memory consolidation (Auto-Dream) ────────────────────

    async def spawn_memory_consolidate(self, host) -> str:
        """触发记忆巩固：curator 子 agent 出 JSON 提案 → 宿主 parse+apply。

        无记忆短路：build_curator_user_message() 返回 "No memory files..." 时不建 subagent，
        直接返回提示。否则分配 curator child session + child-session run_record（单账本，
        `inject_summary=True`：完成摘要 PUSH 回父上下文）+ detached _run_memory_consolidate，
        立即返回 run id 提示（不再镜像 host TaskManager，docs/25 A2）。
        """
        user_message = build_curator_user_message()
        if user_message.startswith("No memory files"):
            return "No memories to consolidate."
        if host._subagents.background_cap_reached():
            return (f"Error: max concurrent sub-agents ({host._subagents.max_threads()}) reached; "
                    f"memory consolidation not started — try again later.")

        child_id = self.new_child_session_id()
        self.record_subagent_spawn_leaf(host, child_id)
        host.emit(SubAgentStarted(agent_type=host._MEMORY_CURATOR_TYPE,
                                  description="memory consolidation"))
        task = asyncio.create_task(host._run_memory_consolidate(
            agent_id=child_id, user_message=user_message))
        task._nanocode_run_id = child_id
        host._background_tasks.add(task)
        task.add_done_callback(host._background_tasks.discard)
        return (f"Started memory consolidation run {child_id}. It will report completion later "
                f"(summary auto-injected on completion; run_status {child_id} to inspect the "
                f"proposal + outcome).")

    async def run_memory_consolidate(self, host, *, agent_id: str,
                                      user_message: str, timeout_ms: int | None = None) -> None:
        """detached 协程：判断型(curator)+确定性(Python apply)解耦。单账本 = child-session
        run_record（docs/25 A2，不再镜像 host TaskManager）。

        **不复用** _run_background_subagent（后者把子文本当最终 result；巩固需 parse+apply
        后处理）。直接 build_profile(memory-curator)（恒无工具）+ _build_sub_agent(background=True)。
        四态对称：cancel/timeout/error 写终态 run_record；成功则 token 累加 + parse(坏 JSON →
        completed "no changes")+apply → summary_line 写入 run_record.resultSummary
        （`inject_summary=True`，完成后摘要 PUSH 回父上下文）。
        """
        sub_agent = None
        description = "memory consolidation"
        eff_model = host.model
        try:
            profile = build_profile(host._MEMORY_CURATOR_TYPE)
            eff_model = profile.model or host.model
            sub_agent = host._build_sub_agent(
                system_prompt=profile.prompt,
                tools=child_tools(host, profile, background=True),
                agent_type=host._MEMORY_CURATOR_TYPE,
                background=True,
                max_turns=host._subagents.bounded_max_turns(profile.max_turns),
                artifact_id=agent_id,
                model=profile.model,
                agent_source=profile.source,
            )
            self.begin_run_record(
                host, sub_agent=sub_agent, agent_id=agent_id,
                agent_type=host._MEMORY_CURATOR_TYPE,
                description=description, prompt=user_message, model=eff_model, background=True,
                context_mode="fresh", isolation="shared", worktree_path=None,
                inject_summary=True)
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    sub_agent.run_once(user_message), timeout=timeout_ms / 1000.0)
            else:
                result = await sub_agent.run_once(user_message)
        except asyncio.CancelledError:
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="cancelled",
                    result_text=host._subagent_captured_text(sub_agent) or "(cancelled)",
                    error="cancelled", result_summary="(cancelled by task_stop)")
                host._close_child_session(agent_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            raise
        except asyncio.TimeoutError:
            text = (
                host._subagent_captured_text(sub_agent)
                if sub_agent is not None else f"(timed out after {timeout_ms}ms)"
            )
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="timed_out",
                    result_text=text, error=None,
                    result_summary=f"(timed out after {timeout_ms}ms)")
                host._close_child_session(agent_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            return
        except Exception as e:
            text = host._subagent_captured_text(sub_agent) if sub_agent is not None else f"(curator error: {e})"
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="failed",
                    result_text=text, error=str(e),
                    result_summary=f"(curator error: {e})")
                host._close_child_session(agent_id, sub_agent)
            else:
                self.create_failed_run_record(
                    host, child_session_id=agent_id,
                    agent_type=host._MEMORY_CURATOR_TYPE,
                    description=description, prompt=user_message, model=eff_model, background=True,
                    context_mode="fresh", isolation="shared",
                    worktree_path=None, error=str(e))
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            return

        # curator 成功产出 JSON 提案：token 累加 + 确定性 parse+apply（宿主 Python，可回滚）。
        # 坏 JSON 不让 run failed，标 completed "no changes"。
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        try:
            plan = parse_consolidation_plan(text)
        except Exception:
            self.finish_run_record(
                sub_agent=sub_agent, status="completed", result_text=text,
                tokens=result["tokens"],
                result_summary="Consolidation: no changes (unparseable plan)")
            host._close_child_session(agent_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))
            return

        apply_result = apply_plan(plan)
        self.finish_run_record(
            sub_agent=sub_agent, status="completed", result_text=text,
            tokens=result["tokens"], result_summary=apply_result.summary_line())
        host._close_child_session(agent_id, sub_agent)
        host.emit(SubAgentEnded(agent_type=host._MEMORY_CURATOR_TYPE, description=description))

    # ─── Memory eval candidate generation (EVAL-mode curator) ──

    async def spawn_memory_eval(self, host) -> str:
        """触发 eval 候选生成：EVAL-mode curator 子 agent 出候选 JSON →
        宿主逐条 add_pending（非法跳过）。无记忆短路。单账本 = child-session run_record
        （`inject_summary=True`），不再镜像 host TaskManager（docs/25 A2）。"""
        from ..memory.eval_source import build_eval_curator_message
        svc = getattr(host, "_memory_service", None)
        backend = getattr(svc, "backend", None) if svc is not None else None
        user_message = build_eval_curator_message(backend)
        if user_message.startswith("No memories"):
            return "No memories to generate eval candidates from."
        if host._subagents.background_cap_reached():
            return (f"Error: max concurrent sub-agents ({host._subagents.max_threads()}) reached; "
                    f"memory eval not started — try again later.")
        # eval 候选 provenance 的 source.session_id 必须指向真实存在的 session，
        # 否则 add_pending 校验会拒掉全部候选。REPL 命令不走 chat()，在此显式落盘。
        host._persist_state()

        child_id = self.new_child_session_id()
        self.record_subagent_spawn_leaf(host, child_id)
        host.emit(SubAgentStarted(agent_type=host._MEMORY_EVAL_CURATOR_TYPE,
                                  description="memory eval generation"))
        task = asyncio.create_task(host._run_memory_eval(
            agent_id=child_id, user_message=user_message))
        task._nanocode_run_id = child_id
        host._background_tasks.add(task)
        task.add_done_callback(host._background_tasks.discard)
        return (f"Started memory eval generation run {child_id}. It will report completion later "
                f"(summary auto-injected on completion; run_status {child_id} to inspect "
                f"generated candidates).")

    async def run_memory_eval(self, host, *, agent_id: str,
                               user_message: str, timeout_ms: int | None = None) -> None:
        """detached 协程：curator 出候选 JSON → 宿主逐条 eval_store.add_pending。单账本 =
        child-session run_record（docs/25 A2，不再镜像 host TaskManager）。

        宿主强制 source.session_id = host.session_id（不信任 curator）。校验失败的
        候选计入 skipped，不让 run failed。坏 JSON → completed 0 candidates。"""
        from ..memory import eval_store
        sub_agent = None
        description = "memory eval generation"
        eff_model = host.model
        try:
            profile = build_profile(host._MEMORY_EVAL_CURATOR_TYPE)
            eff_model = profile.model or host.model
            sub_agent = host._build_sub_agent(
                system_prompt=profile.prompt,
                tools=child_tools(host, profile, background=True),
                agent_type=host._MEMORY_EVAL_CURATOR_TYPE,
                background=True,
                max_turns=host._subagents.bounded_max_turns(profile.max_turns),
                artifact_id=agent_id,
                model=profile.model,
                agent_source=profile.source,
            )
            self.begin_run_record(
                host, sub_agent=sub_agent, agent_id=agent_id,
                agent_type=host._MEMORY_EVAL_CURATOR_TYPE,
                description=description, prompt=user_message, model=eff_model, background=True,
                context_mode="fresh", isolation="shared", worktree_path=None,
                inject_summary=True)
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    sub_agent.run_once(user_message), timeout=timeout_ms / 1000.0)
            else:
                result = await sub_agent.run_once(user_message)
        except asyncio.CancelledError:
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="cancelled",
                    result_text=host._subagent_captured_text(sub_agent) or "(cancelled)",
                    error="cancelled", result_summary="(cancelled by task_stop)")
                host._close_child_session(agent_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
            raise
        except asyncio.TimeoutError:
            text = (
                host._subagent_captured_text(sub_agent)
                if sub_agent is not None else f"(timed out after {timeout_ms}ms)"
            )
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="timed_out",
                    result_text=text, error=None,
                    result_summary=f"(timed out after {timeout_ms}ms)")
                host._close_child_session(agent_id, sub_agent)
            host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
            return
        except Exception as e:
            text = host._subagent_captured_text(sub_agent) if sub_agent is not None else f"(eval curator error: {e})"
            if sub_agent is not None:
                self.finish_run_record(
                    sub_agent=sub_agent, status="failed",
                    result_text=text, error=str(e),
                    result_summary=f"(eval curator error: {e})")
                host._close_child_session(agent_id, sub_agent)
            else:
                self.create_failed_run_record(
                    host, child_session_id=agent_id,
                    agent_type=host._MEMORY_EVAL_CURATOR_TYPE,
                    description=description, prompt=user_message, model=eff_model, background=True,
                    context_mode="fresh", isolation="shared",
                    worktree_path=None, error=str(e))
            host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))
            return

        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""

        # 确定性后处理：解析候选并逐条 add_pending（坏 JSON / 缺 candidates → 0）。
        added = 0
        skipped = 0
        try:
            from ..memory.maintenance import extract_json_object
            data = json.loads(extract_json_object(text))
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
        except Exception:
            self.finish_run_record(
                sub_agent=sub_agent, status="completed", result_text=text,
                tokens=result["tokens"],
                result_summary="Generated 0 pending eval candidates (unparseable output)")
            host._close_child_session(agent_id, sub_agent)
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
        self.finish_run_record(
            sub_agent=sub_agent, status="completed", result_text=text,
            tokens=result["tokens"], result_summary=summary)
        host._close_child_session(agent_id, sub_agent)
        host.emit(SubAgentEnded(agent_type=host._MEMORY_EVAL_CURATOR_TYPE, description=description))

    # ─── Memory optimization (EvolveMem) ──────────────────────
    # Optimization is owned by the memory-evolution system extension. There is
    # intentionally no SubAgentRunner optimize path and no vendored fallback.

    async def run_reserved_agent(self, host, *, agent_type: str, prompt: str,
                                 model: "str | None" = None,
                                 timeout_ms: "int | None" = None) -> str:
        """Reserved-agent spawn（如 memory retrieval diagnostician）。

        走与普通 subagent 一致的 child-session run_record 体系（**无 host task**，
        故不再触 TaskManager）。四态对称：completed 返回 text；cancelled/timeout/error
        先写终态 run_record + 折 token + close child，再向上抛（调用方按 best-effort 处理）。
        """
        profile = build_profile(agent_type)
        eff_model = model or profile.model or host.model
        description = agent_type
        child_id = self.new_child_session_id()
        self.record_subagent_spawn_leaf(host, child_id)
        sub_agent = None
        try:
            sub_agent = host._build_sub_agent(
                system_prompt=profile.prompt,
                tools=child_tools(host, profile, background=True),
                agent_type=agent_type, background=True,
                max_turns=host._subagents.bounded_max_turns(profile.max_turns or 1),
                model=model, artifact_id=child_id, agent_source=profile.source)
            self.begin_run_record(
                host, sub_agent=sub_agent, agent_id=child_id, agent_type=agent_type,
                description=description, prompt=prompt, model=eff_model, background=True,
                context_mode="fresh", isolation="shared", worktree_path=None)
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    sub_agent.run_once(prompt), timeout=timeout_ms / 1000.0)
            else:
                result = await sub_agent.run_once(prompt)
        except asyncio.CancelledError:
            if sub_agent is not None:
                host._fold_subagent_tokens(sub_agent)
                self.finish_run_record(
                    sub_agent=sub_agent, status="cancelled",
                    result_text=host._subagent_captured_text(sub_agent) or "(cancelled)",
                    error="cancelled")
                host._close_child_session(child_id, sub_agent)
            raise
        except asyncio.TimeoutError:
            if sub_agent is not None:
                host._fold_subagent_tokens(sub_agent)
                self.finish_run_record(
                    sub_agent=sub_agent, status="timed_out",
                    result_text=host._subagent_captured_text(sub_agent)
                    or f"(timed out after {timeout_ms}ms)", error=None)
                host._close_child_session(child_id, sub_agent)
            raise
        except Exception as e:
            if sub_agent is not None:
                host._fold_subagent_tokens(sub_agent)
                self.finish_run_record(
                    sub_agent=sub_agent, status="failed",
                    result_text=host._subagent_captured_text(sub_agent)
                    or f"(reserved-agent error: {e})", error=str(e))
                host._close_child_session(child_id, sub_agent)
            else:
                self.create_failed_run_record(
                    host, child_session_id=child_id, agent_type=agent_type,
                    description=description, prompt=prompt, model=eff_model, background=True,
                    context_mode="fresh", isolation="shared", worktree_path=None, error=str(e))
            raise
        host.total_input_tokens += result["tokens"]["input"]
        host.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        self.finish_run_record(
            sub_agent=sub_agent, status="completed", result_text=text, tokens=result["tokens"])
        host._close_child_session(child_id, sub_agent)
        return text
