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

import time

from ..session import v2 as _session_v2


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
            sub._tree_session_id = self.child_session_id(host, artifact_id)
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
                "provider": self.current_provider(host),
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
        return self.write_agent_result(host, agent_id, partial or reason)
