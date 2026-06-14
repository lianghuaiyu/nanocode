"""nanocode 的 Agent 主循环：双后端（Anthropic + OpenAI 兼容）、流式、
多层上下文压缩、Plan Mode、子 Agent、预算控制。"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

from ..tools import (
    tool_definitions,
    execute_tool,
    PermissionEngine,
    ToolDef,
)
from .events import (
    AssistantDelta,
    ApprovalRequested,
    NoticeRaised,
    SubAgentEnded,
    SubAgentStarted,
    ToolBlocked,
    ToolCallAuthorized,
)
from ..prompt import build_system_prompt
from ..skills.discovery import (
    register_nested_skill_dirs,
    path_activates_skill,
    discover_skills,
    reset_skill_cache,
)
from ..mcp import McpManager
from ..tasks.manager import TaskManager
from ..tasks.models import TERMINAL_TASK_STATUSES
from ..tasks.runner import run_shell_background_task
from ..session import v2 as _session_v2

from .models import (
    _get_context_window,
    _model_supports_thinking,
    _model_supports_adaptive_thinking,
)
from .compaction import persist_large_result
from .plan_mode import PlanModeMixin
from .core import AgentCore


# ─── Agent ───────────────────────────────────────────────────

# 子 agent 策略（并发/深度/超时/turn 上限）已抽入 subagent_manager（CAP-P1）。
from .subagent_manager import SubAgentManager  # noqa: E402

# 永不经 execute_tool/mcp、且对持久状态无副作用的纯宿主 meta 工具——P4 allowlist 对
# 这些放行（它们要么是只读任务面板，要么是 plan-mode 状态切换）。
# Sub-agent call-time allowlist 的 meta 工具集与判定已上移至 tools.permissions
# （ALWAYS_ALLOWED_META / AGENT_META_TOOL / allowlist_blocks），由 PermissionEngine 统一持有。


# docs/15 Phase 6：子 agent 构造 + 产物落盘搬迁到 runtime/spawn.py（host-driven SubAgentRunner）。
from ..runtime.spawn import SubAgentRunner  # noqa: E402


class Agent(PlanModeMixin):
    # 记忆巩固 curator 的内置保留类型（与 subagents.prompts.MEMORY_CURATOR_TYPE 对齐）。
    _MEMORY_CURATOR_TYPE = "memory-curator"
    # 记忆 EVAL-mode curator 的内置保留类型（与 subagents.prompts.MEMORY_EVAL_CURATOR_TYPE 对齐）。
    _MEMORY_EVAL_CURATOR_TYPE = "memory-eval-curator"

    def __init__(
        self,
        *,
        permission_mode: str = "default",
        model: str = "claude-opus-4-6",
        api_base: str | None = None,
        anthropic_base_url: str | None = None,
        api_key: str | None = None,
        thinking: bool = False,
        max_cost_usd: float | None = None,
        max_turns: int | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
        custom_system_prompt: str | None = None,
        custom_tools: list[ToolDef] | None = None,
        is_sub_agent: bool = False,
        trajectory_enabled: bool = False,
        trajectory_level: str = "summary",
        workspace_trusted: bool = True,
        task_manager: "TaskManager | None" = None,
        session_id: str | None = None,
        confirmed_paths: set[str] | None = None,
        memory_backend: "MemoryBackend | None" = None,
        artifact_id: str | None = None,
        allowed_tool_names: set[str] | None = None,
        depth: int = 0,
        agent_type: str | None = None,
        agent_source: str | None = None,
    ):
        self.permission_mode = permission_mode
        # 构造时配置的 baseline permission_mode（plan toggle 前）：rebind_session 切 session 时
        # 据此复位——plan 是 session 工作态、不应跨会话（docs/14 P2 review）。
        self._base_permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.use_openai = bool(api_base)
        self.is_sub_agent = is_sub_agent
        # P4 call-time allowlist：本 agent 准许运行的**真实**工具名集合。
        # None = 不约束（主 agent 恒 None）；子 agent 由 _build_sub_agent 传入其有效
        # 工具名集（{t['name'] for t in cfg['tools']}，已剔除 agent + disallowed）。
        # _execute_tool_call 在 meta 工具拦截之后、真实工具派发之前据此 fail-closed。
        self._allowed_tool_names = allowed_tool_names
        # 工具派发的单一决策点（policy + sub-agent allowlist）；读 agent live 上下文。
        self.permission = PermissionEngine(self)
        # 代际深度：主 agent = 0，每下一层子 agent = 父 + 1。max_depth 纵深防御据此判定。
        self.depth = depth
        # 子 agent 身份（审批 UI / 诊断用）：类型 + 来源（自定义项目 agent 的 .md 路径）。
        self.agent_type = agent_type
        self.agent_source = agent_source
        # None = 主 agent 默认全量工具表；[] = 子 agent 显式空工具集（绝不回退全量，
        # 否则 deny-all / allowed-tools:agent / extends 空交集 / curator(tools:[]) 会被
        # 提权为全部工具，含 agent）。
        self.tools = tool_definitions if custom_tools is None else custom_tools
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self.workspace_trusted = workspace_trusted
        self.effective_window = _get_context_window(model) - 20000
        self.session_id = session_id or uuid.uuid4().hex[:8]
        # artifact_id：本 agent 全部产物（messages/meta/prompt/result）的目录键。
        # 主 agent 默认 "main"；子 agent 由 _build_sub_agent 传入其 SubAgentRecord id。
        self.artifact_id = artifact_id or "main"
        # docs/13 cutover S1：主 agent 持一个 SessionManager，每条消息在 message-end 写进
        # canonical session.jsonl 树（干净原文；注入是 render-time 装饰、不入树）。lazy 创建。
        self._session_mgr = None
        # docs/14 full-P6b：_tree_session_id 是 _session_mgr 写入的 session id，与 session_id（artifact/
        # trajectory 目录键）**解耦**。主 agent 二者相同；子 agent 由 _build_sub_agent 置为 child sid，
        # 使子 agent 把自己的 transcript 写进独立的 child session.jsonl（artifacts 仍 parent-keyed，
        # trajectory_id 仍 traj_<parent>，不触 1173-1188 不变量）。_child_parent_session = child header 血缘。
        self._tree_session_id = self.session_id
        self._child_parent_session = None
        if not self.is_sub_agent:
            os.environ["NANOCODE_SESSION_ID"] = self.session_id
        # trajectory 采集开关（docs/10）：plain attrs，gate `nanocode trajectory export`（从 canonical
        # 树派生，B2）。子 agent 经 _build_sub_agent 继承这两个开关。
        self.trajectory_enabled = trajectory_enabled
        self.trajectory_level = trajectory_level
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.current_turns = 0
        self.last_api_call_time = 0.0

        # Abort support
        self._aborted = False
        self._current_task: asyncio.Task | None = None

        # docs/16 #4：typed AgentEvent push 订阅者（RuntimeThread tap 等）。emit 的第三条扇出腿；
        # fire-and-forget——订阅者异常绝不影响 turn。
        self._event_subscribers: list = []

        # docs/17 Phase 0：final_response / 子 agent 文本捕获从 emit 流派生（取代 BufferSink）。
        # emit() 见 AssistantDelta.text 即 append；run_once / RuntimeThread.run 入口 reset
        # （复刻 BufferSink.reset 的每轮语义）。每个 Agent 实例各持一份——子 agent 是独立实例，
        # 故捕获天然隔离，无需按身份过滤。
        self._final_text_chunks: list[str] = []

        # docs/16 #6（STEP E）：per-turn 上下文状态。_turn_context_plan = 本 turn 的 volatile packs
        # （date/git…，request-local 置尾、不入树）；_context_ledger = 本 turn 全量记账（/context）。
        # 都由 AgentSession.run_turn 每 turn 重置。
        self._turn_context_plan = None
        self._context_ledger = None

        # Background tasks (shell) — TaskManager shared with sub-agents via ctor param
        self.task_manager = task_manager if task_manager is not None else TaskManager()
        self._background_tasks: set[asyncio.Task] = set()
        # CAP-P1：子 agent 并发/深度/超时/turn 上限策略归口（Agent 持有并委托）。
        self._subagents = SubAgentManager(self)
        # docs/15 Phase 6：子 agent 构造 + 产物落盘机器（host-driven）。无状态,可共享。
        self._spawn = SubAgentRunner()
        self._agent_session_obj = None     # lazy AgentSession（docs/16 #1：record_event 唯一 message 树写者）
        # docs/15 Phase 5：工具派发单一入口（allowlist 咽喉点 + meta/agent/skill/real 路由 + hooks）。
        from ..capabilities import CapabilityRouter
        self._router = CapabilityRouter()
        # docs/14 §6b（additive child-session）：spawn 时记下父 leaf，finalize 镜像 child session 时
        # 作 parentSession.entryId（pin 到 spawn 分支）。agent_id → 父 spawn leaf。
        self._subagent_spawn_leaf: dict = {}

        # Permission whitelist (shared with sub-agents via ctor param)
        self._confirmed_paths: set[str] = confirmed_paths if confirmed_paths is not None else set()

        # Plan mode state
        self._pre_plan_mode: str | None = None
        self._plan_file_path: str | None = None
        self._plan_approval_fn: Callable[[str], Awaitable[dict]] | None = None
        # turn-scoped context-break 信号（docs/16 #3c，取代 flat 时代的 _context_cleared）：
        # plan clear-and-execute 经 agent_session.clear_for_plan_execution 置位，loop 经
        # cfg.consume_context_break 消费；每 turn 开始复位，绝不跨 turn。
        self._pending_context_break: bool = False

        # Thinking mode
        self._thinking_mode = self._resolve_thinking_mode()

        # docs/17 Phase 0：子 agent 输出捕获经 _final_text_chunks 累加器（见 _captured_text）。

        # Read-before-edit: track file read timestamps (absolutePath → mtime)
        self._read_file_state: dict[str, float] = {}

        # 宿主派生事实：本 agent 实例观测到的 read / modified 文件集合。
        # 子 agent 各自跟踪自己的——run_once 后父读取 sub_agent._files_read/_files_modified
        # 装配 AgentResult。绝不信任模型自述的文件清单。
        self._files_read: set[str] = set()
        self._files_modified: set[str] = set()

        # MCP integration
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        # Memory recall state — semantic prefetch per user turn
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0
        self._memory_backend = memory_backend

        # 渐进披露：已播报的 skill 名 + 待投递的 skill body
        self._sent_skill_names: set[str] = set()
        self._pending_skill_bodies: list[tuple[str, str]] = []
        # paths 条件激活：触碰匹配文件后激活的 skill 名（供下一轮清单可见）
        self._activated_path_skills: set[str] = set()

        # 工具级 hooks：skill 调用时注册的 pre/post-tool-use 条目 + 递归 guard
        self._active_hooks: list[dict] = []
        self._suppress_hooks: bool = False

        # provider 消息列表已退役（docs/16 #3c）：树是会话事实源，每个请求经
        # AgentSession.project_request() 从 build_context 重渲染（request-local ProviderProjection），
        # 不再持任何 durable provider-shaped 列表。

        # Build system prompt
        self._base_system_prompt = custom_system_prompt or build_system_prompt()
        self._apply_permission_mode_prompt()

        # Initialize clients. Provider SDK imports are intentionally lazy so
        # embedding/import-boundary users can construct runtime/test Agents
        # without installing every provider package.
        if self.use_openai:
            try:
                import openai
            except ModuleNotFoundError:
                openai = None
            if openai is None:
                self._openai_client = None
            else:
                self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            self._anthropic_client = None
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            try:
                import anthropic
            except ModuleNotFoundError:
                anthropic = None
            self._anthropic_client = (None if anthropic is None
                                      else anthropic.AsyncAnthropic(**kwargs))
            self._openai_client = None

        # docs/15 STEP B：provider 流式 + capture 收敛到 ProviderAdapter（backend mixin 经 self._provider
        # .stream 委托,engine 不再持 _call_*_stream）。clients 在 rebind 不变,故 _provider 跨 rebind 有效。
        from .providers import make_provider_adapter
        self._provider = make_provider_adapter(
            use_openai=self.use_openai,
            anthropic_client=self._anthropic_client, openai_client=self._openai_client)
        # docs/15 STEP C：模型循环上移到 AgentCore（host=self 注入 collaborators）。无状态,可共享。
        self._core = AgentCore()

    def _apply_permission_mode_prompt(self) -> None:
        """按当前 permission_mode 设 _plan_file_path + _system_prompt（__init__ 与 rebind_session 共用）。
        plan 模式：_system_prompt = base + plan 提示（文本内嵌 plan-<sid>.md 路径）；否则 = base。"""
        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt

    def _resolve_thinking_mode(self) -> str:
        if not self.thinking:
            return "disabled"
        if not _model_supports_thinking(self.model):
            return "disabled"
        if _model_supports_adaptive_thinking(self.model):
            return "adaptive"
        return "enabled"

    @property
    def is_processing(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    def _build_side_query(self):
        """Build a sideQuery callable for memory recall, works with both backends."""
        if self._anthropic_client:
            client = self._anthropic_client
            model = self.model
            async def _sq(system: str, user_message: str) -> str:
                resp = await client.messages.create(
                    model=model, max_tokens=256, system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                return "".join(b.text for b in resp.content if b.type == "text")
            return _sq
        if self._openai_client:
            client = self._openai_client
            model = self.model
            async def _sq_oai(system: str, user_message: str) -> str:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                )
                return resp.choices[0].message.content or "" if resp.choices else ""
            return _sq_oai
        return None

    async def _recall_memory_semantic(self, query: str, limit: int = 5) -> str:
        """memory 工具的语义档：复用 side_query LLM 选记忆。无 client 时回退关键词档。"""
        from ..memory import select_relevant_memories
        from ..tools import memory_tool
        sq = self._build_side_query()
        if sq is None or not query.strip():
            return memory_tool._recall_keyword(query, limit)
        try:
            hits = await select_relevant_memories(query, sq, set())
        except Exception:
            return memory_tool._recall_keyword(query, limit)
        if not hits:
            # 语义档返回空：可能是 LLM 故障（select_* 内部吞异常返回 []），也可能真无匹配。
            # 回退关键词档兜底——它要么找到，要么如实说无匹配。
            return memory_tool._recall_keyword(query, limit)
        out = [f"Top {min(limit, len(hits))} memories for: {query}"]
        for m in hits[:limit]:
            out.append(f"\n{m.header}\n{m.content}")
        return "\n".join(out)

    def abort(self) -> None:
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn: Callable[[str], Awaitable[dict]]) -> None:
        self._plan_approval_fn = fn

    def get_token_usage(self) -> dict:
        return {"input": self.total_input_tokens, "output": self.total_output_tokens}

    # ─── Main entry point ────────────────────────────────────

    async def chat(self, user_message: str) -> None:
        """公开 turn 入口：委托 AgentSession.run_turn（turn shell，docs/16 #3c）。
        取消吞成 _aborted=True 并正常返回的契约由 run_turn 保持。"""
        await self.agent_session.run_turn(user_message)

    # ─── docs/15 Phase 3 / docs/16 #3b：session-context 注入（项目指令 + memory 静态段）已迁入
    # AgentSession.inject_session_context（turn shell 职责），chat() 经 agent_session 调用。

    # ─── Sub-agent entry point ────────────────────────────────

    async def run_once(self, prompt: str) -> dict:
        # 每轮入口重置捕获——复刻旧 `_output_buffer = []`，使复用的（持久/resume/headless）
        # 子 agent 实例不把上一轮文本泄漏进本轮结果（Codex review P2）。
        self.reset_final_text()
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        return {
            "text": self._captured_text(),
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out,
            },
        }

    # ─── Output helper ────────────────────────────────────────

    def reset_final_text(self) -> None:
        """清空 final-text 累加器（docs/17 Phase 0）——每轮入口调用（run_once / RuntimeThread.run）。"""
        self._final_text_chunks = []

    def final_text(self) -> str:
        """本轮累积的 assistant 文本（从 emit 的 AssistantMessageCompleted 流派生）。"""
        return "".join(self._final_text_chunks)

    def _captured_text(self) -> str:
        """子 agent 累积的助手文本（docs/17 Phase 0：从 emit 流派生，不再读 BufferSink）。"""
        return self.final_text()

    @staticmethod
    def _subagent_captured_text(sub_agent) -> str:
        """父读取子 agent 已捕获的 partial 文本（超时/错误终态用）——经子的 sink，
        不再 reach 进已删除的 _output_buffer 字段。"""
        if sub_agent is None:
            return ""
        return sub_agent._captured_text()

    def _emit_block(self, text: str) -> None:
        self.emit(AssistantDelta(text=text))

    # ─── REPL commands ────────────────────────────────────────
    # docs/16 #3b：clear_history 迁入 AgentSession（/clear 经 agent_session.clear_history()）。

    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        self.emit(NoticeRaised(text=f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}"))

    def _get_current_cost_usd(self) -> float:
        return (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    # docs/16 #3a：compaction 流程（阈值门 + entry 写入）归 AgentSession（turn shell / compaction
    # owner）；engine 只保留两个 summarizer LLM 调用点作 per-instance monkeypatch 锚（实现在 AgentCore，
    # summarizer 输入 = 树渲染）。/compact 与 auto-compact 都走 agent_session.compact()。
    async def _compact_anthropic(self, messages: "list | None") -> "str | None":
        return await self._core._compact_anthropic(self, messages)

    async def _compact_openai(self, messages: "list | None") -> "str | None":
        return await self._core._compact_openai(self, messages)

    # ─── Message-list ownership：已退役（docs/16 #3c）─────────────────────────
    # _anthropic_messages/_openai_messages/_active_messages/_load_messages/_dump_messages/
    # _get_message_count 全部删除——树是唯一事实源，请求是 request-local 投影
    # （AgentSession.project_request），任何"装载/导出 flat 列表"的 ownership 语义不复存在。

    # ─── Session ──────────────────────────────────────────────
    # docs/14 SessionLease：`restore_session` 已退役。resume 由 runtime 激活会话写者租约
    # （SessionLease.open_or_create，cli._load_from_manager 渲染初始上下文）完成——canonical 树是
    # 唯一权威，不再读 legacy flat 快照（legacy 导入面已删，docs/16 C-3）。
    # v2 state 的 TaskManager 重载/mark-lost 仍由 _reload_task_state 承担（被 rebind + cli 激活调用）。

    def _reload_task_state(self, state) -> None:
        """load 一个 session 的 v2 state 进 task_manager，并把非终态 task/subagent 标 "lost"
        （进程已不在跑它们）。rebind_session 与 cli 的 SessionLease 激活共用。state 缺/非 dict → no-op。"""
        if not (state and isinstance(state, dict)):
            return
        self.task_manager.load_state(state)
        for t in self.task_manager.list_tasks():
            if t.status not in TERMINAL_TASK_STATUSES:
                self.task_manager.update_task(t.id, status="lost")
        for a in self.task_manager.list_subagents():
            if a.status in ("running", "idle"):
                self.task_manager.update_subagent(a.id, status="lost")

    # ─── Runtime replacement：原地重指 session（docs/14 P2）────────────────────────

    def _reset_working_sets(self) -> None:
        """复位 session 维度的 working set（rebind_session 用——切到新 session 不应继承旧 session 的
        审批白名单 / 读文件状态 / 已播报 skill / 已浮现 memory 等）。plan/permission 态另由
        _reset_session_mode 处理（保持单一职责，避免 docstring 与实现漂移，docs/14 P2 review）。

        注意：与 clear_history 的复位面**刻意不同**——clear_history 只清对话 + 部分 skill 态，
        保留 _confirmed_paths/_read_file_state/memory（同一 session 内清屏）；rebind 是换 session 需全清。"""
        self._sent_skill_names = set()
        self._pending_skill_bodies = []
        self._activated_path_skills = set()
        self._active_hooks = []
        self._confirmed_paths.clear()           # 与子 agent 共享的同一 set（切 session 时无 live 子 agent）
        self._read_file_state = {}
        self._files_read = set()
        self._files_modified = set()
        self._already_surfaced_memories = set()
        self._session_memory_bytes = 0
        reset_skill_cache()

    def _reset_session_mode(self) -> None:
        """把 permission/plan 态复位到构造时 baseline（rebind 用）。plan 是 session 工作态、不跨会话：
        新 session 要么回到 baseline 非-plan 模式，要么（若启动即 --plan）以**新 sid** 的 plan 文件/提示
        重新进入——等价于以同一 config 全新构造一个 agent。修复 P2 review 的 plan-mode 跨会话泄漏。"""
        self.permission_mode = self._base_permission_mode
        self._pre_plan_mode = None
        self._pending_context_break = False
        self._apply_permission_mode_prompt()    # 按 baseline mode 重算 _plan_file_path + _system_prompt（新 sid）

    def rebind_session(self, new_mgr, *, artifact_id: str = "main") -> None:
        """原地把**主** agent 重指到 new_mgr 所属的 session：finalize 旧 session 的全部 session-keyed
        状态，再 rebuild 新 session 的。复用同一 Agent 实例（保留 MCP/memory/clients/tools/system_prompt/
        审批回调），只换 session 维度——使 /new /resume /clone 与子父导航共用一条原子替换路径。

        docs/14 SessionLease：ownership 上移到 runtime 层。`new_mgr` 必须是 runtime 的 `SessionLease`
        持有的**已加锁、已 build_context 校验过**的 SessionManager（acquire-validate-new-before-
        release-old 的 fail-closed 闸在 `_switch_via_rebind` 里完成，busy/corrupt 时根本不会走到这里）。
        rebind 自身不再 open/lock——只 finalize 旧、装载新。new_mgr.session_id==当前 sid → no-op。
        fail-closed 前置（turn/后台/子 agent 运行中拒绝）由 RuntimeHost.can_switch 在调用前保证。"""
        if self.is_sub_agent:
            raise RuntimeError("rebind_session is for the main agent only")
        new_sid = new_mgr.session_id
        if new_sid == self.session_id:
            return
        old_sid = self.session_id
        built = new_mgr.build_context()         # 已由 _switch_via_rebind 校验过；纯内存 fold，无 I/O
        # ── FINALIZE 旧 session（均为低风险/guarded 操作）──
        old_mgr = self._session_mgr
        self.agent_session.auto_save()          # 旧 session 的 v2 state
        if old_mgr is not None and old_mgr is not new_mgr:
            old_mgr.close()                     # 释放旧 session 写锁（旧 lease 的底层 mgr）
        try:
            from ..tools.sandbox_shell import cleanup_persist_sandbox
            cleanup_persist_sandbox(old_sid)    # 旧 persist sandbox + fingerprint
        except Exception:
            pass
        # ── REBUILD 新 session ──
        self.session_id = new_sid
        self._tree_session_id = new_sid         # 主 agent：tree sid == session sid（保持同步）
        self.artifact_id = artifact_id
        os.environ["NANOCODE_SESSION_ID"] = new_sid
        self._session_mgr = new_mgr             # runtime lease 持有的已加锁 mgr
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # task_manager：fresh + load 目标 session 的 state + 非终态标 lost
        self.task_manager = TaskManager()
        self._subagents = SubAgentManager(self)
        self._reload_task_state(_session_v2.read_state(new_sid) if _session_v2.is_v2_session(new_sid) else None)
        # 计数复位（新 session 从零计 cost/turns）
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.current_turns = 0
        self._aborted = False
        self._reset_working_sets()
        self._reset_session_mode()              # plan/permission 复位到 baseline（recompute _system_prompt，新 sid）
        # 请求是 request-local 投影（每轮从新 session 树重渲染，docs/16 #3c）——无需装载 flat 列表。
        self.emit(NoticeRaised(text=f"Session → {new_sid} ({len(built.messages)} messages)."))

    # docs/16 #3b：_auto_save 迁入 AgentSession.auto_save（chat/rebind 经 agent_session 调用）。

    def _ensure_session_lease(self) -> None:
        """确保本 agent 持有一把会话写者租约（已加锁的 SessionManager）——在每个 turn 开始处调用。

        docs/14 SessionLease：写者身份归 runtime 的 active-thread lease。生产路径（CLI/REPL/一次性/
        子 agent spawn）由 runtime 经 `SessionLease` 注入 `_session_mgr`，此处即 no-op。headless / SDK /
        直接构造（含测试）无 runtime 注入时，在 turn 活动期自取一把**加锁** lease（经
        `SessionLease.open_or_create`，绝非未加锁 create、绝非 flat fallback）。取锁时机在 turn 活动期、
        而非 `__init__`——构造模型 core 不决定写者身份、不占 fd。"""
        if self._session_mgr is not None:
            return
        from ..session.lease import SessionLease
        self._session_mgr = SessionLease.open_or_create(
            self._tree_session_id, parent_session=self._child_parent_session).manager

    @property
    def agent_session(self):
        """本 agent 的 AgentSession（state↔tree 同步边界）。docs/16 #1：message family 的树写入
        统一经 agent_session.record_event（required=True fail-loud），AgentCore 不再内联 _tree_record。"""
        if self._agent_session_obj is None:
            from .session import AgentSession
            self._agent_session_obj = AgentSession(self)
        return self._agent_session_obj

    def emit(self, event) -> bool:
        """docs/16 #2：**单一事件出口**——一条 typed AgentEvent 扇出
        `[agent_session.record_event（canonical 树）, _event_subscribers（订阅者 push）]`。
        树先于 UI：required 写失败 fail-loud 时不画半截 UI。返回 record_event 的写入结果
        （ContextInjected 调用方据此推进 dedup）。

        docs/17 Phase 1-4：UI 投影腿（project_agent_event → EventSink）已删——所有表现
        （assistant/tool/spinner/cost/info/retry/sub_agent/approval）由订阅端 TerminalClient 从
        事件流渲染（TUI 客户端化）。core 只 emit、不再认识表现层。"""
        written = self.agent_session.record_event(event)
        if isinstance(event, AssistantDelta) and event.text:
            # docs/17 Phase 0：final_response / 子 agent 文本从 emit 流派生（取代 BufferSink）。
            # 仅 text block（不含 thinking），多轮 turn 累计、每轮入口 reset；每个 Agent 实例各持
            # 一份，子 agent 是独立实例故捕获天然隔离。
            self._final_text_chunks.append(event.text)
        for listener in list(self._event_subscribers):
            try:
                listener(event)
            except Exception:
                pass   # fire-and-forget（docs/16 #4）：push 订阅者绝不反向破坏 turn
        return written

    # docs/16 #3b：_tree_record/_tree_event/_tree_custom_message/_build_request_messages 已迁入
    # AgentSession（record_event 的树腿 + build_request_messages）——engine 不再直接写树。

    def _persist_state(self) -> None:
        """Write v2 state (tasks + subagents) to disk —— DERIVED cache（非 resume 权威，docs/14 P7）。
        canonical 树是会话事实源；这里只落 TaskManager/subagent 生命周期记录供 /resume 重载 + mark-lost。
        无读者键 session_id/startTime 已删（docs/16 C-2 随 #3）。"""
        try:
            _session_v2.write_state(self.session_id, self.task_manager.to_state())
        except Exception as e:
            # docs/16 #2：silent pass 已清——derived cache 落盘失败必须可观测（不破坏 live turn）。
            self.emit(NoticeRaised(text=f"[state] v2 state persist failed: {e}"))

    # ─── Autocompact ──────────────────────────────────────────
    # docs/16 #3a：_check_and_compact / _compact_conversation 已迁入 AgentSession
    # （check_and_compact / compact）——compaction owner = turn shell，entry 写入经
    # CompactionRequested→record_event 单写者。

    # ─── Skill progressive disclosure ─────────────────────────
    # docs/16 #3b：_skill_listing_budget/_tree_custom_message/_inject_skill_listing/
    # _inject_pending_skill_bodies/_inject_finished_tasks 已迁入 AgentSession（注入器 = turn shell 职责，
    # 树是唯一注入通道）；core loop 经 host.agent_session.inject_* 调用。

    def _on_file_touched(self, name: str, inp: dict) -> None:
        """成功 read/write/edit 后触发：宿主派生文件事实 + 嵌套发现 .nanocode/skills + paths 条件激活。

        name 是工具名（read_file/write_file/edit_file）：read_file → _files_read，
        write_file/edit_file → _files_modified（绝对化路径，宿主**观测**派生，不信任模型）。
        """
        fp = inp.get("file_path")
        if not fp:
            return
        # 宿主派生事实：记录被触碰的文件（按工具语义分到 read / modified）。
        try:
            abspath = str(Path(fp).resolve())
        except Exception:
            abspath = str(fp)
        if name == "read_file":
            self._files_read.add(abspath)
        elif name in ("write_file", "edit_file"):
            self._files_modified.add(abspath)
        touched = Path(fp)
        cwd = Path.cwd()
        register_nested_skill_dirs(touched, cwd)        # 先嵌套发现
        for s in discover_skills():                      # 再 paths 激活
            if s.paths and path_activates_skill(touched, s, cwd):
                self._activated_path_skills.add(s.name)

    # ─── Large result persistence ─────────────────────────────────

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        return persist_large_result(tool_name, result)

    # ─── Execute tool (handles agent/skill/plan mode internally) ─────

    async def _spawn_background_shell(self, command: str, timeout_ms: int | None) -> str:
        rec = self.task_manager.create_task("shell", command, owner_agent_id=None)
        d = _session_v2.task_dir(self.session_id, rec.id)
        stdout_path = str(d / "stdout.log"); stderr_path = str(d / "stderr.log")
        self.task_manager.update_task(rec.id, stdout_path=stdout_path, stderr_path=stderr_path)
        task = asyncio.create_task(run_shell_background_task(
            self.task_manager, rec.id, command, stdout_path, stderr_path, timeout_ms,
            session_id=self.session_id))
        task._nanocode_task_id = rec.id
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return rec.id

    async def spawn_background_shell(self, command: str, timeout_ms: int | None) -> str:
        return await self._spawn_background_shell(command, timeout_ms)

    def list_tasks(self, status=None, kind=None) -> str:
        from ..tools.tasks_tool import list_tasks_text
        return list_tasks_text(self.task_manager, status, kind)

    def task_output(self, task_id: str, tail_bytes: int = 8000) -> str:
        from ..tools.tasks_tool import task_output_text
        return task_output_text(self.task_manager, task_id, tail_bytes)

    async def stop_task(self, task_id: str) -> str:
        from ..tools.tasks_tool import task_stop
        return await task_stop(
            self.task_manager, self._background_tasks, task_id,
            allow_orphan_cancel=not self.is_sub_agent)

    def _tool_blocked_by_allowlist(self, name: str) -> bool:
        """P4 call-time allowlist 判定——委托给 PermissionEngine（单一决策来源）。

        语义见 tools.permissions.allowlist_blocks。保留本薄包装供 callgate
        (_execute_tool_call) 与 hook-shell 路径 (_run_hook) 调用，二者即 fail-closed 兜底点。
        """
        return self.permission.allowlist_blocks(name)

    def tool_blocked_by_allowlist(self, name: str) -> bool:
        return self._tool_blocked_by_allowlist(name)

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        """工具派发 callgate（实现搬迁到 capabilities.CapabilityRouter.dispatch；以下为薄委托）。

        allowlist fail-closed 咽喉点 + meta/agent/skill/real 路由 + hooks 都在 router；engine 保此入口供
        backend loop（host._execute_tool_call）+ 早执行 + tests 调用。"""
        return await self._router.dispatch(self, name, inp)

    async def _run_real_tool(self, name: str, inp: dict) -> str:
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        result = await execute_tool(name, inp, self._read_file_state)
        if name in ("read_file", "write_file", "edit_file") and not result.startswith(("Error", "Warning")):
            self._on_file_touched(name, inp)
        return result

    async def run_real_tool(self, name: str, inp: dict) -> str:
        return await self._run_real_tool(name, inp)

    async def recall_memory_semantic(self, query: str, limit: int = 5) -> str:
        return await self._recall_memory_semantic(query, limit)

    async def spawn_memory_consolidate(self) -> str:
        return await self._spawn_memory_consolidate()

    async def execute_plan_mode_tool(self, name: str) -> str:
        return await self._execute_plan_mode_tool(name)

    async def execute_agent_tool(self, inp: dict) -> str:
        return await self._execute_agent_tool(inp)

    async def execute_skill_tool(self, inp: dict) -> str:
        return await self._execute_skill_tool(inp)

    # ─── 工具级 hooks：注册 / 匹配 / 执行 ──────────────────────

    def _register_skill_hooks(self, sk) -> None:
        """把 skill frontmatter 的 hooks 注册到 _active_hooks（去重）。"""
        for event, entries in (sk.hooks or {}).items():
            for e in entries:
                rec = {"skill": sk.name, "event": event, "matcher": e["matcher"],
                       "command": e["command"], "timeout_ms": e["timeout_ms"]}
                if rec not in self._active_hooks:
                    self._active_hooks.append(rec)

    def _matching_hooks(self, event: str, tool_name: str) -> list[dict]:
        from ..skills.hooks import hook_matches
        return [h for h in self._active_hooks
                if h["event"] == event and hook_matches(h["matcher"], tool_name)]

    def hooks_suppressed(self) -> bool:
        return self._suppress_hooks

    def has_active_hooks(self) -> bool:
        return bool(self._active_hooks)

    def matching_hooks(self, event: str, tool_name: str) -> list[dict]:
        return self._matching_hooks(event, tool_name)

    async def _run_hook(self, h: dict, tool_name: str, inp: dict, result):
        """执行一条 hook；返回 (ok, message)。命令走统一 check_permission：
        deny→阻断，confirm→前台询问/后台自动拒，bypass 下危险命令仍硬底线阻断。"""
        import json
        from ..tools import run_shell, check_permission
        from ..skills.hooks import build_hook_event

        cmd = h["command"]
        # P4 安全基石：skill hook 会以 run_shell 身份跑命令。若本（子）agent 的有效集
        # 不含 run_shell，则它不得借 skill hook 旁路获得 shell——按 allowlist fail-closed。
        if self._tool_blocked_by_allowlist("run_shell"):
            self.emit(ToolBlocked(tool="run_shell", reason="hook_not_in_allowlist"))
            return False, (f"hook command blocked: run_shell is not permitted for this sub-agent "
                           f"({h['skill']} {h['event']})")
        perm = check_permission("run_shell", {"command": cmd}, self.permission_mode)
        if perm["action"] == "deny":
            return False, f"hook command denied ({h['skill']} {h['event']}): {perm.get('message', cmd)}"
        if perm["action"] == "confirm":
            approved = await self._confirm_dangerous(f"skill hook {h['skill']} {h['event']}: {cmd}")
            if not approved:
                return False, f"hook command not approved ({h['skill']} {h['event']}): {cmd}"
        elif perm["action"] == "allow" and run_shell.is_dangerous(cmd):
            # allow + 危险命令（bypassPermissions，或显式 allow 规则命中）：
            # hook 不得借此无人值守地跑危险命令——硬底线阻断
            return False, f"hook command blocked by safety backstop (dangerous under {self.permission_mode}): {cmd}"

        event = build_hook_event(h["event"], h["skill"], tool_name, inp,
                                 (result or "")[:2000] if result else None,
                                 str(Path.cwd()), self.session_id)
        # 权限通过后，用统一 planner 决定执行方式：任何沙盒档（auto/seatbelt）下 hook 都在
        # 原生 OS 沙盒内受限跑（写 workspace 受限、无网，宿主工具链在），无原生后端则 blocked；
        # off 档 hook 仍宿主跑（off=不沙盒）。hook 绝不进 microVM、绝不裸跑沙盒归类的命令。
        hook_inp = {"command": cmd, "timeout": h["timeout_ms"], "stdin": json.dumps(event),
                    "_session_id": self.session_id}
        kind, info = run_shell.plan_shell(hook_inp, context="hook")
        self._suppress_hooks = True
        try:
            if kind == "blocked":
                return False, f"hook blocked: {info}"
            if kind == "sandbox":
                # info 是后端模块：run_structured 接受 stdin（hook event JSON）。
                r = info.run_structured(hook_inp, posture="workspace-write", cwd=str(Path.cwd()))
            else:  # host（off 档 / escalate）
                r = run_shell.run_structured(hook_inp)
        finally:
            self._suppress_hooks = False
        if r["timed_out"]:
            return False, f"hook timed out after {h['timeout_ms']}ms"
        if r["error"] is not None:
            return False, f"hook error: {r['error']}"
        if r["exit_code"] != 0:
            out = (r["stderr"] or r["stdout"] or "").strip()[:500]
            return False, f"exit {r['exit_code']}: {out}"
        return True, ""

    async def run_hook(self, h: dict, tool_name: str, inp: dict, result):
        return await self._run_hook(h, tool_name, inp, result)

    # ─── Sub-agent factory (centralized permission inheritance) ──

    def _parent_remaining_turns(self) -> int | None:
        """父若有 max_turns 预算，返回剩余可用 turn 数（>=0），否则 None（无界）。"""
        if self.max_turns is None:
            return None
        return max(0, self.max_turns - self.current_turns)

    # ─── P4 concurrency / depth caps：策略在 SubAgentManager（self._subagents），调用方直连（docs/16 C-1）──

    def _build_sub_agent(self, *, system_prompt, tools, agent_type, session_id=None,
                         background=False, max_turns=None, model=None,
                         artifact_id=None, agent_source=None) -> "Agent":
        """构造子 agent：集中权限继承。

        与 Claude Code / Kimi Code 对齐：
        - 子继承父 permission_mode（硬规则「子不得高于父」，不再无条件 bypass）。
        - 共享父 confirm_fn + _confirmed_paths（确认回流到父，同一引用）。
        - 共享 session_id + task_manager。
        - is_sub_agent 工具表强制剔除 agent（子不能 spawn 孙）。
        - max_turns：前台子 agent 传入有界 turn 上限（_check_budget 强制），保证有界。
        - model：可选 per-agent 模型覆盖（manifest 'model'）；None 则继承父 model。

        P4 call-time allowlist：从**实际**子工具集（已剔除 agent + disallowed）派生
        allowed_tool_names = {t['name'] for t in safe_tools}，传给子 agent。子 agent 据此
        在 _execute_tool_call fail-closed——即便模型臆造一个未播报的真实工具名也跑不了。
        depth = 父 depth + 1（纵深防御计数）。agent_type/agent_source 供审批 UI 标识身份。

        background=True（detached 后台子 agent）：无 TTY，需确认的危险调用一律
        auto-deny（confirm_fn=_auto_deny_confirm 恒拒），并使用**新空集** confirmed_paths
        （不与父共享，后台确认不回流父），其余继承不变。

        docs/15 Phase 6：实现已搬迁到 runtime/spawn.py（host-driven）；此为薄委托。
        """
        return self._spawn.build_sub_agent(
            self, system_prompt=system_prompt, tools=tools, agent_type=agent_type,
            session_id=session_id, background=background, max_turns=max_turns,
            model=model, artifact_id=artifact_id, agent_source=agent_source)

    # ─── Skill fork mode ─────────────────────────────────────

    async def _execute_skill_tool(self, inp: dict) -> str:
        from ..skills import execute_skill, get_skill_by_name
        sk = get_skill_by_name(inp.get("skill_name", ""))
        if sk and sk.disable_model_invocation:
            return f'Skill "{inp.get("skill_name", "")}" cannot be invoked by the model (disable-model-invocation).'
        if sk and sk.hooks:
            self._register_skill_hooks(sk)
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))
        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        if result["context"] == "fork":
            # 安全不变量：子 agent 不得经任何 meta 路径 spawn 后代。'agent' 工具已硬拦，
            # skill fork 同样是「spawn 一个子 agent」——对子 agent 一律禁止（避免借
            # fork-mode skill 绕过「子不 spawn 孙」做纵深 fan-out）。主 agent 不受限。
            if self.is_sub_agent:
                return ("Error: fork-mode skills are not available to sub-agents "
                        "(sub-agents cannot spawn descendants).")
            # max_depth backstop（主 agent 仍受全局深度上限约束）。
            if self._subagents.depth_cap_exceeded():
                return ("Error: max sub-agent depth reached; skill fork not spawned.")
            tools = (
                [t for t in self.tools if t["name"] in result["allowed_tools"]]
                if result.get("allowed_tools")
                else [t for t in self.tools if t["name"] != "agent"]
            )
            skill_name = inp.get("skill_name", "")
            fork_prompt = inp.get("args") or "Execute this skill task."
            # 每次 fork 注册独立 SubAgentRecord → 各自的 artifact_id/dir，
            # 避免多次 skill-fork 把产物并入同一个 agents/skill-fork/ 目录。
            rec = self.task_manager.create_subagent(
                type="skill-fork", description=skill_name,
                model=self.model, provider=self._current_provider(),
            )
            self.task_manager.update_subagent(rec.id, status="running")
            self._write_agent_spawn_artifacts(
                agent_id=rec.id, agent_type="skill-fork", description=skill_name,
                prompt=fork_prompt, model=self.model, background=False)
            self.emit(SubAgentStarted(agent_type="skill-fork", description=skill_name))
            sub_agent = None
            try:
                sub_agent = self._build_sub_agent(
                    system_prompt=result["prompt"],
                    tools=tools,
                    agent_type="coder",
                    max_turns=self._subagents.bounded_max_turns(None),
                    artifact_id=rec.id,
                )
                # 经 _await_subagent_run（与前台一致）而非裸 await run_once：
                # chat() 会吞掉 CancelledError，裸 await 会把真实取消误当成功。
                # 此处无 wall-clock 超时，kind=='timeout' 即表示被取消/abort。
                kind, payload = await self._await_subagent_run(sub_agent, fork_prompt, None)
            except asyncio.CancelledError:
                self.task_manager.update_subagent(rec.id, status="cancelled")
                if sub_agent is not None:
                    self._close_child_session(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "cancelled")
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                raise
            except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
                self.task_manager.update_subagent(rec.id, status="failed")
                if sub_agent is not None:
                    self._close_child_session(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "failed")
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                return f"Skill fork error: {e}"

            if kind == "timeout":
                # 无超时设定 → 'timeout' 表示运行被取消/aborted：落 cancelled 并向上传播取消。
                self.task_manager.update_subagent(rec.id, status="cancelled")
                self._close_child_session(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "cancelled")
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                raise asyncio.CancelledError()
            if kind == "error":
                self.task_manager.update_subagent(rec.id, status="failed")
                self._close_child_session(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "failed")
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                return f"Skill fork error: {payload}"

            sub_result = payload
            self.total_input_tokens += sub_result["tokens"]["input"]
            self.total_output_tokens += sub_result["tokens"]["output"]
            self.task_manager.update_subagent(rec.id, status="completed")
            self._close_child_session(rec.id, sub_agent)
            result_path = self._write_agent_result(rec.id, sub_result["text"] or "")
            self._finalize_agent_meta(rec.id, "completed")
            self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
            # 与 fresh/resume 一致：回传有界信封而非整段 transcript（完整在 result.md）。
            return self._finalize_foreground_result(sub_agent, sub_result, result_path, rec.id)

        self._pending_skill_bodies.append((inp.get("skill_name", ""), result["prompt"]))
        return f'[skill "{inp.get("skill_name", "")}" loaded — its instructions follow in the next message]'

    def _current_provider(self) -> str:
        return self._spawn.current_provider(self)

    def _close_child_session(self, agent_id: str, sub_agent: "Agent") -> None:
        """Close 子 agent 的 child 写者租约（docs/15 Phase 6：实现在 runtime/spawn.py）。"""
        return self._spawn.close_child_session(self, agent_id, sub_agent)

    def child_session_id(self, agent_id: str) -> str:
        """子 agent 的 child session id（docs/14 §6b）。父 sid 作前缀，保证跨父唯一。"""
        return self._spawn.child_session_id(self, agent_id)

    def _write_agent_spawn_artifacts(self, *, agent_id: str, agent_type: str,
                                     description: str, prompt: str, model: str,
                                     background: bool) -> None:
        """子 agent 创建时落 prompt.txt + meta.json + 记 spawn 父 leaf（实现在 runtime/spawn.py）。"""
        return self._spawn.write_agent_spawn_artifacts(
            self, agent_id=agent_id, agent_type=agent_type, description=description,
            prompt=prompt, model=model, background=background)

    def _finalize_agent_meta(self, agent_id: str, status: str) -> None:
        """子 agent 终态补 status + ended_at（实现在 runtime/spawn.py）。"""
        return self._spawn.finalize_agent_meta(self, agent_id, status)

    def _write_agent_result(self, agent_id: str, text: str) -> str | None:
        """子 agent 最终文本 → <agent_dir>/result.md（实现在 runtime/spawn.py）。"""
        return self._spawn.write_agent_result(self, agent_id, text)

    # ─── Structured AgentResult + bounded envelope ────────────────
    # docs/16 #7b：spawn 终态/成功路径改走 typed agents.result.ResultEnvelope；
    # engine 的 _build_agent_result/_render_agent_result_envelope 委托 shim 删除
    # （纯函数本体在 agent_result.py，ResultEnvelope 复用之）。

    def _fold_subagent_tokens(self, sub_agent: "Agent") -> None:
        """把子 agent 已花费的 token 折叠进父（成功/超时/错误都折；实现在 runtime/spawn.py）。"""
        return self._spawn.fold_subagent_tokens(self, sub_agent)

    def _write_terminal_result(self, agent_id: str, sub_agent, reason: str) -> str | None:
        """终态写 result.md：有 partial 输出写它,否则写 reason（实现在 runtime/spawn.py）。"""
        return self._spawn.write_terminal_result(self, agent_id, sub_agent, reason)

    def _finalize_foreground_terminal(self, sub_agent: "Agent", record_id: str,
                                      kind: str, payload, timeout_ms: int | None) -> str:
        """前台 timeout/error 终态共用（实现在 runtime/spawn.py）。"""
        return self._spawn.finalize_foreground_terminal(
            self, sub_agent, record_id, kind, payload, timeout_ms)

    def _finalize_foreground_result(self, sub_agent: "Agent", result: dict,
                                    result_path: str | None, record_id: str | None) -> str:
        """前台/skill-fork 成功路径共用（实现在 runtime/spawn.py）。"""
        return self._spawn.finalize_foreground_result(
            self, sub_agent, result, result_path, record_id)

    # ─── Background sub-agent (detached, auto-deny-but-continue) ──

    def _write_subagent_result(self, task_id: str, text: str) -> str | None:
        """子 agent 完整输出 → task_dir/result.md（实现在 runtime/spawn.py）。"""
        return self._spawn.write_subagent_result(self, task_id, text)

    async def _spawn_background_subagent(self, *, agent_type: str, description: str,
                                         prompt: str, timeout_ms: int | None = None) -> str:
        """注册 subagent + task + detached 协程（实现在 runtime/spawn.py）。"""
        return await self._spawn.spawn_background_subagent(
            self, agent_type=agent_type, description=description, prompt=prompt, timeout_ms=timeout_ms)

    async def _run_background_subagent(self, *, agent_id: str, task_id: str, agent_type: str,
                                       description: str, prompt: str,
                                       timeout_ms: int | None) -> None:
        """detached 后台子 agent 协程（实现在 runtime/spawn.py）。"""
        return await self._spawn.run_background_subagent(
            self, agent_id=agent_id, task_id=task_id, agent_type=agent_type,
            description=description, prompt=prompt, timeout_ms=timeout_ms)

    
    # ─── Memory curator spawns（实现搬迁到 runtime/spawn.py；以下为薄委托）──────────
    async def _spawn_memory_consolidate(self) -> str:
        return await self._spawn.spawn_memory_consolidate(self)

    async def _run_memory_consolidate(self, **kw) -> None:
        return await self._spawn.run_memory_consolidate(self, **kw)

    async def _spawn_memory_eval(self) -> str:
        return await self._spawn.spawn_memory_eval(self)

    async def _run_memory_eval(self, **kw) -> None:
        return await self._spawn.run_memory_eval(self, **kw)

    async def _spawn_memory_optimize(self) -> str:
        return await self._spawn.spawn_memory_optimize(self)

    async def _run_memory_optimize(self, **kw) -> None:
        return await self._spawn.run_memory_optimize(self, **kw)

    async def _await_subagent_run(self, sub_agent: "Agent", prompt: str,
                                  timeout_ms: int | None) -> tuple[str, object]:
        """可靠 await 子 agent run_once + 超时判定（实现在 runtime/spawn.py;§7 风险#1 cancel 语义保留）。"""
        return await self._spawn.await_subagent_run(sub_agent, prompt, timeout_ms)

    async def _run_foreground_subagent(self, sub_agent: "Agent", prompt: str,
                                       timeout_ms: int | None,
                                       record_id: str | None) -> tuple[str, str | dict]:
        """前台子 agent 执行 + 超时（实现在 runtime/spawn.py）。"""
        return await self._spawn.run_foreground_subagent(
            self, sub_agent, prompt, timeout_ms, record_id)

    async def _execute_agent_tool(self, inp: dict) -> str:
        """`agent` 工具主入口（fresh/resume/background 分派；实现在 runtime/spawn.py）。"""
        return await self._spawn.execute_agent_tool(self, inp)

    async def _authorize_dispatch(self, name: str, inp: dict) -> "tuple[bool, str | None]":
        """两后端派发前共用的授权 + 审批交互（单一入口，de-dup）。返回 ``(allowed, denial)``。

        - 经 ``self.permission.check`` 取 policy 决策并写树 ``permission_decision``；
        - ``deny`` → 打印并返回 ``"Action denied: …"``；
        - ``confirm`` → 走 ``_confirm_if_needed``（dedupe + 身份装饰）；拒则返回固定文案。

        **allowlist 不在此判**——它是 ``_execute_tool_call`` 的 fail-closed 兜底（保持子 agent
        拒绝消息 "Error: tool '…' is not permitted" 与 prompt-then-block 行为不变）。
        """
        d = self.permission.check(name, inp)
        self.emit(ToolCallAuthorized(tool=name, action=d.action, message=d.message))
        if d.action == "deny":
            return False, f"Action denied: {d.message}"
        if d.action == "confirm" and d.message:
            if not await self._confirm_if_needed(d.message):
                return False, "User denied this action."
        return True, None

    async def _confirm_dangerous(self, command: str) -> bool:
        # P4 审批 UI 身份：子 agent 触发的确认必须带上子 agent 身份（id + type + source），
        # 否则审批人无从判断是哪个（可能由不受信项目定义的）子 agent 在请求危险操作。
        # 仅 enrich 传给 confirm_fn 的消息字符串，签名不变；主 agent 行为不变。
        message = self._decorate_confirm_message(command)
        # docs/17 Phase 4：审批显示经事件流（订阅端 TerminalClient 渲染告警），决策经注入的
        # confirm_fn 往返。未注入 confirm_fn → fail-closed deny（取代旧阻塞 input() 回退——
        # core 绝不直接读 stdin；headless/RPC 必须显式注入审批回调）。
        self.emit(ApprovalRequested(command=command, message=message,
                                    request_id=uuid.uuid4().hex[:8]))
        if self.confirm_fn:
            return await self.confirm_fn(message)
        return False

    def _confirm_dedupe_key(self, message: str) -> str:
        """共享 _confirmed_paths 的去重键：子 agent 按身份隔离，避免「A 批过的危险命令
        被 B 静默复用、跳过带身份的确认」。主 agent 用原始消息（不变）。"""
        return self._decorate_confirm_message(message)

    async def _confirm_if_needed(self, message: str) -> bool:
        """统一的「危险动作确认 + 共享去重」决策——两后端共用（端到端可测、防变异）。

        返回 True=放行（此前已按本 agent 身份确认过，或刚确认通过）；False=拒绝。
        去重键经 _confirm_dedupe_key 按子 agent 身份隔离：兄弟子 agent 的批准互不复用。"""
        if not message:
            return True
        key = self._confirm_dedupe_key(message)
        if key in self._confirmed_paths:
            return True
        if not await self._confirm_dangerous(message):
            return False
        self._confirmed_paths.add(key)
        return True

    def _decorate_confirm_message(self, command: str) -> str:
        """子 agent 确认消息前缀加身份标识（[sub-agent <id> type=<t> source=<s>]）。
        主 agent（非子）原样返回。"""
        if not self.is_sub_agent:
            return command
        parts = [f"sub-agent {self.artifact_id}"]
        if self.agent_type:
            parts.append(f"type={self.agent_type}")
        if self.agent_source:
            parts.append(f"source={self.agent_source}")
        return f"[{' '.join(parts)}] {command}"
