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
    REGISTRY,
    execute_tool,
    PermissionEngine,
    ToolDef,
)
from ..capabilities.validation import validate_tool_input
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


def _embedder_overlay(embedder_tools: "list | None") -> list:
    """嵌入者工具（AgentConfig.tools）→ 规范化为 source=EMBEDDER、name=embedder__<name> 的 Tool 列表
    （docs/24 §4.5 / Phase 4b）。

    嵌入者传 Tool 对象（schema+run）；此处强制 source=EMBEDDER、名加 embedder__ 前缀（已带则不重复
    加），trust 由嵌入者在 Tool 上声明（默认 UNTRUSTED）。这些只进 per-agent overlay，绝不写全局
    REGISTRY；UNTRUSTED ⟹ dispatch 铸 ctx 时能力槽全 None。"""
    if not embedder_tools:
        return []
    from dataclasses import replace as _replace
    from ..tools.types import ToolSource
    out: list = []
    for t in embedder_tools:
        name = t.name
        schema = t.schema
        if not name.startswith("embedder__"):
            schema = dict(t.schema)
            schema["name"] = f"embedder__{name}"
        out.append(_replace(t, schema=schema, source=ToolSource.EMBEDDER))
    return out

# 永不经 execute_tool/mcp、且对持久状态无副作用的纯宿主 meta 工具——P4 allowlist 对
# 这些放行（它们要么是只读任务面板，要么是 plan-mode 状态切换）。
# Sub-agent call-time allowlist 的 meta 工具集与判定已上移至 tools.permissions
# （ALWAYS_ALLOWED_META / AGENT_META_TOOL / allowlist_blocks），由 PermissionEngine 统一持有。


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
        memory_service: "MemoryService | None" = None,
        artifact_id: str | None = None,
        allowed_tool_names: set[str] | None = None,
        depth: int = 0,
        agent_type: str | None = None,
        agent_source: str | None = None,
        sandbox_profile: str = "default",
        embedder_tools: "list | None" = None,
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
        # docs/24 Phase 4a/4b：本 agent 持自己的 overlay registry（builtins + 本 agent 的外部工具）。
        # 外部工具（MCP 每会话 / 扩展每 runtime / 嵌入者每 config）绝不写进全局 REGISTRY——
        # 它们只进 per-agent overlay（跨会话不泄漏 / 不重复注册炸）。嵌入者工具（AgentConfig.tools，
        # source=EMBEDDER、name=embedder__name）在构造时叠加；MCP（首 turn）/ 扩展（apply services）
        # 后续 register。self.tools = self._registry.schemas()。
        self._registry = REGISTRY.overlay(_embedder_overlay(embedder_tools))
        self.tools = self._registry.schemas() if custom_tools is None else custom_tools
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self.workspace_trusted = workspace_trusted
        # docs/19：sandbox profile（default/read-only/strict/vm/danger-full-access）。
        # profile 是 public runtime config（AgentConfig.sandbox_profile）；模型无法影响。
        self._sandbox_profile = sandbox_profile
        # docs/23 Step 7-S4：SandboxManager 归 runtime 所有（RuntimeServices.sandbox，per-runtime
        # 共享、无状态）——主 agent 经 _apply_runtime_services、子 agent 经 build_sub_agent 采用该实例。
        # 此处自建一个作兜底（未经 runtime 装配的白盒/测试 agent）；彻底移除待 S6（__init__ 接 bundle）。
        from ..capabilities.sandbox import SandboxManager
        self._sandbox = SandboxManager()
        self.effective_window = _get_context_window(model) - 20000
        self.session_id = session_id or uuid.uuid4().hex[:8]
        # artifact_id：主 agent 默认 "main"；子 agent 传入 child session id。
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

        # docs/18 Phase 1：auto-compaction 熔断状态（CompactionPolicy 阈值在 AgentSession）。
        # _compacting：reentrancy 守卫（compaction 进行中 → check_and_compact 不重入）；
        # _consecutive_compaction_failures：连续 auto-compaction 失败计数，达 policy.max 后跳过
        # 本 session 后续 auto compact（手动 /compact 不受限）。会话维度状态 → rebind 时复位。
        self._compacting = False
        self._consecutive_compaction_failures = 0
        # docs/18 Phase 6：per-message 聚合 tool-result 预算的替换决策（按 toolCallId 冻结，跨 turn
        # 稳定，保护 prompt-cache 前缀）。请求局部投影，绝不改写树；会话维度 → rebind 复位。
        from .tool_result_budget import ContentReplacementState
        self._content_replacement = ContentReplacementState()

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
        if task_manager is not None:
            self.task_manager = task_manager
        else:
            from ..tasks.manager import TaskManager
            self.task_manager = TaskManager()
        self._background_tasks: set[asyncio.Task] = set()
        self._background_run_queue: list[str] = []
        # CAP-P1：子 agent 并发/深度/超时/turn 上限策略归口（Agent 持有并委托）。
        self._subagents = SubAgentManager(self)
        from ..runs.runtime import AgentRunRuntime
        self._run_runtime = AgentRunRuntime()
        # docs/15 Phase 6：子 agent 构造 + 产物落盘机器（host-driven）。无状态,可共享。
        # docs/23 §4.1/§4.2：runtime/spawn 是 L4 host 服务（③）；core(②a/②b) 不在模块顶层
        # 静态依赖 ③ 实现——lazy import 保 `import nanocode.agent.engine` 不拖入 nanocode.runtime
        # （与同构造器内 AgentRunRuntime 的 lazy import 同风格；行为不变，构造点与时机一致）。
        from ..runtime.spawn import SubAgentRunner
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
        from ..mcp import McpManager
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        # Memory recall state — no-LLM fast prefetch per user turn.
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0
        self._memory_service = memory_service

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
            anthropic_client=self._anthropic_client, openai_client=self._openai_client,
            registry=self._registry)
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
        if self._memory_service is None:
            return "Memory is not available for this agent."
        return await self._memory_service.execute_tool(
            {"action": "search", "query": query, "limit": limit}, host=self)

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

    async def _chat_internal(self, user_message: str) -> None:
        """内部 turn 入口：委托 AgentSession.run_turn（turn shell，docs/16 #3c）。

        docs/23 Phase 4：外部/生产调用方必须经 RuntimeThread.run()；这是 run_once 等内部
        路径专用的薄 helper，不再作公开 turn 入口。取消吞成 _aborted=True 并正常返回的契约
        由 run_turn 保持。"""
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
        await self._chat_internal(prompt)
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
    async def _compact_anthropic(self, messages: "list | None",
                                 instructions: str | None = None) -> "str | None":
        return await self._core._compact_anthropic(self, messages, instructions)

    async def _compact_openai(self, messages: "list | None",
                              instructions: str | None = None) -> "str | None":
        return await self._core._compact_openai(self, messages, instructions)

    async def _summarize_anthropic(self, messages: "list | None", prompt_text: str) -> "str | None":
        return await self._core._summarize_anthropic(self, messages, prompt_text)

    async def _summarize_openai(self, messages: "list | None", prompt_text: str) -> "str | None":
        return await self._core._summarize_openai(self, messages, prompt_text)

    # ─── Message-list ownership：已退役（docs/16 #3c）─────────────────────────
    # _anthropic_messages/_openai_messages/_active_messages/_load_messages/_dump_messages/
    # _get_message_count 全部删除——树是唯一事实源，请求是 request-local 投影
    # （AgentSession.project_request），任何"装载/导出 flat 列表"的 ownership 语义不复存在。

    # ─── Session ──────────────────────────────────────────────
    # docs/14 SessionLease：`restore_session` 已退役。resume 由 runtime 激活会话写者租约
    # （SessionLease.open_or_create，cli._load_from_manager 渲染初始上下文）完成——canonical 树是
    # 唯一权威，不再读 legacy flat 快照（legacy 导入面已删，docs/16 C-3）。
    # ─── Runtime replacement：原地重指 session（docs/14 P2）────────────────────────

    def rebind_session(self, new_mgr, *, artifact_id: str = "main") -> None:
        """原地把**主** agent 重指到 new_mgr 所属的 session：finalize 旧 session 的全部 session-keyed
        状态，再 rebuild 新 session 的。复用同一 Agent 实例（保留 MCP/memory/clients/tools/system_prompt/
        审批回调），只换 session 维度——使 /new /resume /clone 与子父导航共用一条原子替换路径。

        docs/14 SessionLease：ownership 上移到 runtime 层。`new_mgr` 必须是 runtime 的 `SessionLease`
        持有的**已加锁、已 build_context 校验过**的 SessionManager（acquire-validate-new-before-
        release-old 的 fail-closed 闸在 `_switch_via_rebind` 里完成，busy/corrupt 时根本不会走到这里）。
        rebind 自身不再 open/lock——只 finalize 旧、装载新。new_mgr.session_id==当前 sid → no-op。
        fail-closed 前置（turn/后台/子 agent 运行中拒绝）由 RuntimeHost.can_switch 在调用前保证。"""
        from ..runtime.rebind import rebind_agent_session
        rebind_agent_session(self, new_mgr, artifact_id=artifact_id)

    # docs/16 #3b：_auto_save 迁入 AgentSession.auto_save（chat/rebind 经 agent_session 调用）。

    def _ensure_session_lease(self) -> None:
        """确保本 agent 持有一把会话写者租约（已加锁的 SessionManager）——在每个 turn 开始处调用。

        docs/14 SessionLease / docs/23 Phase 4：写者身份归 runtime 的 active-thread lease。生产路径
        （CLI/REPL/一次性/子 agent spawn）由 runtime 经 `SessionLease` 注入 `_session_mgr`（spawn 给
        子 agent 注入 child 租约），此处即 no-op。缺注入时 **fail loud**——绝不自取一把 lease，因为
        自取会绕开 runtime 的单写者所有权。白盒测试经 `tests._helpers.attach_runtime_agent` 显式注入。"""
        if self._session_mgr is None:
            raise RuntimeError(
                "No active session writer lease. Start the agent through AgentRuntime.")

    @property
    def agent_session(self):
        """本 agent 的 AgentSession（state↔tree 同步边界）。docs/16 #1：message family 的树写入
        统一经 agent_session.record_event（required=True fail-loud），AgentCore 不再内联 _tree_record。"""
        if self._agent_session_obj is None:
            from ..session.agent import AgentSession
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
        from ..skills.discovery import (
            register_nested_skill_dirs,
            path_activates_skill,
            discover_skills,
        )
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
        # docs/19：后台 shell 也经唯一规划点 SandboxManager。HostContext(is_background=True) 在 spawn
        # 时快照（cwd/session 固定）；microVM/无后端 → blocked（fail-closed，不裸跑）。
        # background 不支持 escalate（permission 层已拒 run_in_background+escalate）→ approval 恒不批。
        from ..capabilities.sandbox import ShellRequest, ApprovalDecision
        from ..tasks.runner import run_shell_background_task
        request = ShellRequest(command=command, timeout_ms=timeout_ms or 0, run_in_background=True)
        host = self.host_context(background=True)
        policy = self.sandbox_policy(background=True)
        task = asyncio.create_task(run_shell_background_task(
            self.task_manager, self._sandbox, rec.id, request, host, policy,
            ApprovalDecision(approved=False), stdout_path, stderr_path))
        task._nanocode_task_id = rec.id
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return rec.id

    async def spawn_background_shell(self, command: str, timeout_ms: int | None) -> str:
        return await self._spawn_background_shell(command, timeout_ms)

    # ─── docs/19：HostContext / SandboxPolicy 提供面（runtime 注入,非模型）────────────

    def _effective_cwd(self) -> str:
        services = getattr(self, "_runtime_services", None)
        if services is not None:
            return services.cwd
        if self._session_mgr is not None:
            return self._session_mgr._cwd()
        return os.getcwd()

    def host_context(self, *, background: bool = False, hook: bool = False):
        """构造 SandboxManager 规划所需的 HostContext。cwd/session/workspace/身份/interactive
        全部由 runtime 决定，模型无法影响（docs/19 §4.1）。"""
        from ..capabilities.sandbox import HostContext
        cwd_p = Path(os.path.realpath(self._effective_cwd()))
        temps: list[Path] = []
        seen: set[str] = set()
        for cand in (os.environ.get("TMPDIR"), "/tmp"):
            if cand and os.path.isdir(cand):
                rp = Path(os.path.realpath(cand))
                if str(rp) not in seen and str(rp) != str(cwd_p):
                    seen.add(str(rp))
                    temps.append(rp)
        return HostContext(
            cwd=cwd_p, session_id=self.session_id, workspace_roots=(cwd_p,),
            temp_roots=tuple(temps),
            interactive=bool(self.confirm_fn) and not background,
            is_subagent=self.is_sub_agent, is_background=background, is_hook=hook,
            approval_mode=self.permission_mode)

    def sandbox_policy(self, *, background: bool = False, hook: bool = False):
        """据 profile + HostContext 投影 SandboxPolicy。子 agent / hook 只收窄不放宽（docs/19 §8）。"""
        from ..capabilities.sandbox import policy_for_profile, narrow_policy_for_context
        host = self.host_context(background=background, hook=hook)
        policy = policy_for_profile(self._sandbox_profile, host)
        return narrow_policy_for_context(policy, host)

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

    def _live_run_ids(self) -> set[str]:
        return {
            rid for rid in (
                getattr(task, "_nanocode_run_id", None)
                for task in self._background_tasks
                if not task.done()
            )
            if rid
        }

    def _reconcile_run(self, child_session_id: str):
        from ..runs.models import TERMINAL_RUN_STATUSES
        rec = self._run_runtime.status(child_session_id)
        if rec.status not in TERMINAL_RUN_STATUSES and child_session_id not in self._live_run_ids():
            rec = self._run_runtime.mark_lost(
                child_session_id,
                reason="no live coroutine in current runtime",
            )
        return rec

    def run_list(self, status: str | None = None) -> str:
        import json
        records = [
            r.to_dict()
            for r in self._run_runtime.list(
                self.session_id,
                status=status,
                live_run_ids=self._live_run_ids(),
            )
        ]
        return json.dumps(records, ensure_ascii=False, indent=2)

    def run_status(self, child_session_id: str) -> str:
        import json
        try:
            rec = self._reconcile_run(child_session_id)
        except Exception as e:
            return f"Error: {e}"
        return json.dumps(rec.to_dict(), ensure_ascii=False, indent=2)

    def run_output(self, child_session_id: str, include_events: bool = False,
                   tail_events: int = 20) -> str:
        import json
        try:
            self._reconcile_run(child_session_id)
        except Exception as e:
            return f"Error: {e}"
        return json.dumps(
            self._run_runtime.output(child_session_id, include_events=include_events,
                                     tail_events=tail_events),
            ensure_ascii=False,
            indent=2,
        )

    async def run_cancel(self, child_session_id: str) -> str:
        for task in list(self._background_tasks):
            if getattr(task, "_nanocode_run_id", None) == child_session_id:
                task.cancel()
                return f"Requested cancel of run {child_session_id}."
        from ..runs.models import TERMINAL_RUN_STATUSES
        try:
            rec = self._run_runtime.status(child_session_id)
        except Exception as e:
            return f"Error: {e}"
        if rec.status in TERMINAL_RUN_STATUSES:
            return f"Run {child_session_id} is already terminal: {rec.status}."
        self._run_runtime.mark_lost(
            child_session_id,
            reason="cancel requested but no live coroutine found",
        )
        return f"Run {child_session_id}: no live coroutine found; marked lost."

    def run_send(self, child_session_id: str, prompt: str, *,
                 delivery: str = "steer", wake: bool = False) -> str:
        import json
        try:
            self._reconcile_run(child_session_id)
            queued = self._run_runtime.send(child_session_id, prompt, delivery=delivery, wake=wake)
        except Exception as e:
            return f"Error: {e}"
        return json.dumps(queued, ensure_ascii=False, indent=2)

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
        # docs/24 Phase 4b：MCP 路由改为 source 判定（不再 is_mcp_tool 前缀旁路）——MCP 工具已
        # register 进 self._registry（source=MCP）。执行仍经 mcp_manager.call_tool（行为等价）。
        from ..tools.types import ToolSource
        tool = self._registry.get(name)
        if tool is not None and tool.source is ToolSource.MCP:
            return await self._mcp_manager.call_tool(name, inp)
        result = await execute_tool(name, inp, self._read_file_state,
                                    ctx=self._mint_tool_context(name), registry=self._registry)
        if name in ("read_file", "write_file", "edit_file") and not result.startswith(("Error", "Warning")):
            self._on_file_touched(name, inp)
        return result

    @property
    def registry(self):
        """本 agent 的 per-agent overlay registry（docs/24 §4.3 / Phase 4a，ToolHost.registry）。

        router/validation/execute 经此读 schema、解析 tool、铸 ctx——绝不读全局 REGISTRY。
        外部工具只进此 overlay；全局 REGISTRY 运行时永不被改（只读基底）。"""
        return self._registry

    def _granted_capabilities(self, tool):
        """docs/24 §4.3 铸造规则：granted = tool.needs ∩ policy_for_trust(tool.trust)。

        BUILTIN 信任档策略 = 全集 → needs ∩ 全集 = needs(「声明什么给什么」，与今天等价;
        把手仍沙箱中介)。TRUSTED 仅读类、UNTRUSTED 空——外部工具(Phase 4)默认零宿主能力。
        """
        from ..tools.types import policy_for_trust
        return frozenset(tool.needs) & policy_for_trust(tool.trust)

    def _mint_tool_context(self, name: str):
        """咽喉点现场铸造 per-call ToolContext（docs/24 §4.4 ③）。

        按 granted（BUILTIN 信任档「声明什么给什么」）铸造对应把手；未授予的槽为 None。
        - FS_READ → fs_read/fs_list（以 sandbox_policy().filesystem 为依据）
        - FS_WRITE → fs_write
        - EXEC → exec（SandboxManager 前台执行）
        - TASKS → tasks（后台任务面板）
        - SESSION_READ → runs（child-session run record）
        - MEMORY → memory；SPAWN → spawn
        - SET_MODE → set_mode，**但子 agent 时强制 None**（保住 plan-mode 主 agent-only 门）
        把手内部持 host(self) 引用做薄转发；ToolContext 本身无任何字段通向 raw Agent/_session_mgr/lease。"""
        from ..tools.context import (
            ToolContext, FsReadCap, FsWriteCap, FsListCap,
            ExecCap, TasksCap, RunsCap, MemoryCap, SpawnCap, SetModeCap,
        )
        from ..tools.types import Capability
        tool = self._registry.get(name)
        granted = self._granted_capabilities(tool) if tool is not None else frozenset()
        fs_policy = self.sandbox_policy().filesystem
        fs_read = FsReadCap(fs_policy) if Capability.FS_READ in granted else None
        fs_list = FsListCap(fs_policy) if Capability.FS_READ in granted else None
        fs_write = FsWriteCap(fs_policy) if Capability.FS_WRITE in granted else None
        exec_cap = ExecCap(self) if Capability.EXEC in granted else None
        tasks_cap = TasksCap(self) if Capability.TASKS in granted else None
        runs_cap = RunsCap(self) if Capability.SESSION_READ in granted else None
        memory_cap = MemoryCap(self) if Capability.MEMORY in granted else None
        spawn_cap = SpawnCap(self) if Capability.SPAWN in granted else None
        # SET_MODE 仅主 agent：子 agent 时即便声明也不铸造把手（plan-mode 主 agent-only 门）。
        set_mode_cap = (SetModeCap(self)
                        if (Capability.SET_MODE in granted and not self.is_sub_agent)
                        else None)
        return ToolContext(
            call_id="",
            cwd=str(self.host_context().cwd),
            signal=None,
            fs_read=fs_read,
            fs_write=fs_write,
            fs_list=fs_list,
            exec=exec_cap,
            tasks=tasks_cap,
            runs=runs_cap,
            memory=memory_cap,
            spawn=spawn_cap,
            set_mode=set_mode_cap,
        )

    def mint_tool_context(self, name: str):
        """公开 port：dispatch 咽喉点（CapabilityRouter）为 host-routed 工具铸造 per-call ToolContext。

        薄委托 _mint_tool_context（按 granted 铸造能力把手；子 agent set_mode=None）。"""
        return self._mint_tool_context(name)

    async def run_real_tool(self, name: str, inp: dict) -> str:
        result = await self._run_real_tool(name, inp)
        self._mark_external_memory_context(name)
        return result

    def _mark_external_memory_context(self, name: str) -> None:
        svc = self._memory_service
        if svc is None:
            return
        if name in ("web_fetch", "web_search"):
            source = name
        elif name == "tool_search":
            source = "tool_search"
        elif name.startswith("mcp__"):
            source = "mcp"
        else:
            return
        svc.on_external_context_used(source=source, thread_id=self.session_id)

    async def recall_memory_semantic(self, query: str, limit: int = 5) -> str:
        return await self._recall_memory_semantic(query, limit)

    async def execute_memory_tool(self, inp: dict) -> str:
        if self._memory_service is None:
            return "Memory is not available for this agent."
        return await self._memory_service.execute_tool(inp, host=self)

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
        # docs/19：hook 命令经唯一规划点 SandboxManager（HostContext(is_hook=True)）受限执行：
        # 任何沙盒档下都在 native/VM 内跑（写 workspace 受限、无网，宿主工具链在），无后端则 blocked；
        # hook 绝不裸跑宿主（narrow_policy_for_context 把 engine=host 收窄为 auto）。
        from ..capabilities.sandbox import ShellRequest, ApprovalDecision
        request = ShellRequest(command=cmd, timeout_ms=h["timeout_ms"] or 30000,
                               stdin=json.dumps(event))
        self._suppress_hooks = True
        try:
            r = await self._sandbox.execute_structured(
                request, self.host_context(hook=True), self.sandbox_policy(hook=True),
                ApprovalDecision(approved=False))   # hook 永不 escalate
        finally:
            self._suppress_hooks = False
        if r.get("blocked"):
            return False, f"hook blocked: {r['blocked']}"
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
            child_id = self._spawn.new_child_session_id()
            self._spawn.record_subagent_spawn_leaf(self, child_id)
            self.emit(SubAgentStarted(agent_type="skill-fork", description=skill_name))
            sub_agent = None
            try:
                sub_agent = self._build_sub_agent(
                    system_prompt=result["prompt"],
                    tools=tools,
                    agent_type="coder",
                    max_turns=self._subagents.bounded_max_turns(None),
                    artifact_id=child_id,
                )
                self._spawn.begin_run_record(
                    self, sub_agent=sub_agent, agent_id=child_id, agent_type="skill-fork",
                    description=skill_name, prompt=fork_prompt, model=self.model, background=False,
                    context_mode="fresh", isolation="shared", worktree_path=None)
                # 经 _await_subagent_run（与前台一致）而非裸 await run_once：
                # chat() 会吞掉 CancelledError，裸 await 会把真实取消误当成功。
                # 此处无 wall-clock 超时，kind=='timeout' 即表示被取消/abort。
                kind, payload = await self._await_subagent_run(sub_agent, fork_prompt, None)
            except asyncio.CancelledError:
                if sub_agent is not None:
                    self._spawn.finish_run_record(
                        sub_agent=sub_agent, status="cancelled",
                        result_text=self._subagent_captured_text(sub_agent) or "(cancelled)",
                        error="cancelled")
                    self._close_child_session(child_id, sub_agent)
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                raise
            except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
                if sub_agent is not None:
                    self._spawn.finish_run_record(
                        sub_agent=sub_agent, status="failed",
                        result_text=self._subagent_captured_text(sub_agent) or f"Skill fork error: {e}",
                        error=str(e))
                    self._close_child_session(child_id, sub_agent)
                else:
                    self._spawn.create_failed_run_record(
                        self, child_session_id=child_id, agent_type="skill-fork",
                        description=skill_name, prompt=fork_prompt, model=self.model, background=False,
                        context_mode="fresh", isolation="shared", worktree_path=None,
                        error=str(e))
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                return f"Skill fork error: {e}"

            if kind == "timeout":
                # 无超时设定 → 'timeout' 表示运行被取消/aborted：落 cancelled 并向上传播取消。
                self._spawn.finish_run_record(
                    sub_agent=sub_agent, status="cancelled",
                    result_text=self._subagent_captured_text(sub_agent) or "(cancelled)",
                    error="cancelled")
                self._close_child_session(child_id, sub_agent)
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                raise asyncio.CancelledError()
            if kind == "error":
                self._spawn.finish_run_record(
                    sub_agent=sub_agent, status="failed",
                    result_text=self._subagent_captured_text(sub_agent) or f"Skill fork error: {payload}",
                    error=str(payload))
                self._close_child_session(child_id, sub_agent)
                self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
                return f"Skill fork error: {payload}"

            sub_result = payload
            self.total_input_tokens += sub_result["tokens"]["input"]
            self.total_output_tokens += sub_result["tokens"]["output"]
            result_path = self._spawn.finish_run_record(
                sub_agent=sub_agent, status="completed",
                result_text=sub_result["text"] or "", tokens=sub_result["tokens"])
            self._close_child_session(child_id, sub_agent)
            self.emit(SubAgentEnded(agent_type="skill-fork", description=skill_name))
            # 与 fresh/resume 一致：回传有界信封而非整段 transcript（完整在 result.md）。
            return self._finalize_foreground_result(sub_agent, sub_result, result_path, child_id)

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

    # ─── Structured AgentResult + bounded envelope ────────────────
    # docs/16 #7b：spawn 终态/成功路径改走 typed agents.result.ResultEnvelope；
    # engine 的 _build_agent_result/_render_agent_result_envelope 委托 shim 删除
    # （纯函数本体在 agent_result.py，ResultEnvelope 复用之）。

    def _fold_subagent_tokens(self, sub_agent: "Agent") -> None:
        """把子 agent 已花费的 token 折叠进父（成功/超时/错误都折；实现在 runtime/spawn.py）。"""
        return self._spawn.fold_subagent_tokens(self, sub_agent)

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

    async def _spawn_background_subagent(self, *, agent_type: str, description: str,
                                         prompt: str, timeout_ms: int | None = None,
                                         context_mode: str = "fresh",
                                         isolation: str | None = None) -> str:
        """注册 subagent + task + detached 协程（实现在 runtime/spawn.py）。"""
        return await self._spawn.spawn_background_subagent(
            self, agent_type=agent_type, description=description, prompt=prompt,
            timeout_ms=timeout_ms, context_mode=context_mode, isolation=isolation)

    async def _run_background_subagent(self, *, agent_id: str, agent_type: str,
                                       description: str, prompt: str,
                                       timeout_ms: int | None, sub_agent=None,
                                       worktree_path: str | None = None,
                                       queued: bool = False) -> None:
        """detached 后台子 agent 协程（实现在 runtime/spawn.py）。"""
        return await self._spawn.run_background_subagent(
            self, agent_id=agent_id, agent_type=agent_type,
            description=description, prompt=prompt, timeout_ms=timeout_ms,
            sub_agent=sub_agent, worktree_path=worktree_path, queued=queued)

    
    # ─── Memory curator spawns（实现搬迁到 runtime/spawn.py；以下为薄委托）──────────
    async def _spawn_memory_consolidate(self) -> str:
        return await self._spawn.spawn_memory_consolidate(self)

    async def _run_memory_consolidate(self, **kw) -> None:
        return await self._spawn.run_memory_consolidate(self, **kw)

    async def _spawn_memory_eval(self) -> str:
        return await self._spawn.spawn_memory_eval(self)

    async def _run_memory_eval(self, **kw) -> None:
        return await self._spawn.run_memory_eval(self, **kw)

    async def _run_reserved_agent(self, **kw) -> str:
        return await self._spawn.run_reserved_agent(self, **kw)

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
        # docs/19 Phase 1：先 validate 再 permission——permission 必须看到 validated args。
        # docs/24 Phase 4a：schema 查表走 self._registry（per-agent overlay）。
        verr = validate_tool_input(name, inp, registry=self._registry)
        if verr is not None:
            self.emit(ToolCallAuthorized(tool=name, action="deny", message=verr))
            return False, f"Error: {verr}"
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
