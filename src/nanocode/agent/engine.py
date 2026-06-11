"""nanocode 的 Agent 主循环：双后端（Anthropic + OpenAI 兼容）、流式、
多层上下文压缩、Plan Mode、子 Agent、预算控制。"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

import anthropic
import openai

from ..tools import (
    tool_definitions,
    execute_tool,
    PermissionEngine,
    CONCURRENCY_SAFE_TOOLS,
    get_active_tool_definitions,
    ToolDef,
    PermissionMode,
)
from ..memory import (
    start_memory_prefetch,
    format_memories_for_injection,
    MemoryPrefetch,
)
from ..memory.maintenance import (
    build_curator_user_message,
    parse_consolidation_plan,
    apply_plan,
    build_eval_curator_message,
)
from .sink import EventSink, TerminalSink, BufferSink
from ..prompt import build_system_prompt
from ..skills.listing import (
    skill_listing_delta,
    render_skill_body_message,
    append_to_last_user,
)
from ..skills.discovery import (
    register_nested_skill_dirs,
    path_activates_skill,
    discover_skills,
    reset_skill_cache,
)
from ..subagents import get_sub_agent_config
from ..mcp import McpManager
from ..trace import Tracer, JsonlSink, build_default_sinks, is_enabled as _trace_is_enabled
from ..tasks.manager import TaskManager
from ..tasks.models import TERMINAL_TASK_STATUSES
from ..tasks.runner import run_shell_background_task
from ..tasks.inject import render_task_reminder, collect_pending_injections
from ..session import v2 as _session_v2
from ..tools import tasks_tool

from .models import (
    _get_context_window,
    _model_supports_thinking,
    _model_supports_adaptive_thinking,
    _get_max_output_tokens,
    _to_openai_tools,
    _with_retry,
)
from .compaction import persist_large_result
from .plan_mode import PlanModeMixin
from .anthropic_backend import AnthropicBackendMixin
from .openai_backend import OpenAIBackendMixin


# ─── Agent ───────────────────────────────────────────────────

# 子 agent 策略（并发/深度/超时/turn 上限）已抽入 subagent_manager（CAP-P1）。
# SUBAGENT_MAX_TURNS_FALLBACK 随之迁入；此处 import 兼有 re-export 作用（back-compat）。
from .subagent_manager import SubAgentManager, SUBAGENT_MAX_TURNS_FALLBACK  # noqa: E402,F401
from . import agent_result  # noqa: E402 — 子 agent 结果信封纯函数（CAP-P1 STEP 1）
from . import runtime_events  # noqa: E402 — 单一 RuntimeEvent 流（RUNTIME-P1）

# 永不经 execute_tool/mcp、且对持久状态无副作用的纯宿主 meta 工具——P4 allowlist 对
# 这些放行（它们要么是只读任务面板，要么是 plan-mode 状态切换）。
# Sub-agent call-time allowlist 的 meta 工具集与判定已上移至 tools.permissions
# （ALWAYS_ALLOWED_META / AGENT_META_TOOL / allowlist_blocks），由 PermissionEngine 统一持有。


async def _auto_deny_confirm(_command: str) -> bool:
    """后台子 agent 的 confirm_fn：无 TTY 等价拒绝（auto-deny-but-continue）。"""
    return False


class Agent(AnthropicBackendMixin, OpenAIBackendMixin, PlanModeMixin):
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
        trace_enabled: bool = False,
        trace_parent=None,
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
        sink: "EventSink | None" = None,
    ):
        self.permission_mode = permission_mode
        # 构造时配置的 baseline permission_mode（plan toggle 前）：rebind_session 切 session 时
        # 据此复位——plan 是 session 工作态、不应跨会话（docs/14 P2 review）。
        self._base_permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.use_openai = bool(api_base)
        self.is_sub_agent = is_sub_agent
        # 表现层注入边界（P-1 解耦目标3）：core 经 self._sink 发 UI 事件，不直接 import ..ui。
        # 显式注入优先；否则子 agent 用 BufferSink（捕获助手文本、抑制其余），主 agent 用
        # TerminalSink（包装 ui.py，行为不变）。_output_buffer 的旧职责并入 BufferSink。
        if sink is not None:
            self._sink = sink
        elif is_sub_agent:
            self._sink = BufferSink()
        else:
            self._sink = TerminalSink()
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
        # artifact_id：本 agent 全部产物（messages/meta/prompt/result/wire）的目录键。
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
        # trajectory 采集开关（docs/10）：在 _build_tracer 之前置好，供 Tracer 派生 trajectory_id。
        # DERIVED 投影的 wire 整形开关；关闭/FULL 时 payload byte-identical。
        self.trajectory_enabled = trajectory_enabled
        self.trajectory_level = trajectory_level
        # 保存 trace 开关原值：rebind_session（docs/14 P2）切 session 时要用同样的 debug-trace
        # 设置重建 tracer（_build_tracer 内仍按 _trace_is_enabled 复核 flag|env）。
        self._trace_enabled = trace_enabled
        self.tracer = self._build_tracer(trace_enabled=trace_enabled, trace_parent=trace_parent)
        self.tracer.emit(
            "session_start", model=self.model, cwd=str(Path.cwd()),
            permission_mode=self.permission_mode, is_sub_agent=self.is_sub_agent,
            workspace_trusted=self.workspace_trusted,
        )
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.current_turns = 0
        self.last_api_call_time = 0.0

        # Abort support
        self._aborted = False
        self._current_task: asyncio.Task | None = None

        # Background tasks (shell) — TaskManager shared with sub-agents via ctor param
        self.task_manager = task_manager if task_manager is not None else TaskManager()
        self._background_tasks: set[asyncio.Task] = set()
        # CAP-P1：子 agent 并发/深度/超时/turn 上限策略归口（Agent 持有并委托）。
        self._subagents = SubAgentManager(self)
        # docs/14 §6b（additive child-session）：spawn 时记下父 leaf，finalize 镜像 child session 时
        # 作 parentSession.entryId（pin 到 spawn 分支）。agent_id → 父 spawn leaf。
        self._subagent_spawn_leaf: dict = {}

        # Permission whitelist (shared with sub-agents via ctor param)
        self._confirmed_paths: set[str] = confirmed_paths if confirmed_paths is not None else set()

        # Plan mode state
        self._pre_plan_mode: str | None = None
        self._plan_file_path: str | None = None
        self._plan_approval_fn: Callable[[str], Awaitable[dict]] | None = None
        self._context_cleared: bool = False  # Set when plan approval clears context

        # Thinking mode
        self._thinking_mode = self._resolve_thinking_mode()

        # 子 agent 输出捕获已并入注入的 BufferSink（见 self._sink / _captured_text）。

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

        # provider 消息列表（plain list）。docs/13 S5：树是会话事实源，这两个列表降为每轮
        # 从 build_context 渲染的投影（live 请求源见 _build_request_messages）；MessageStore 抽象已删。
        # 仅当前 provider 的列表会被填充（use_openai 决定）。
        self._anthropic_messages: list = []
        self._openai_messages: list = []

        # Build system prompt
        self._base_system_prompt = custom_system_prompt or build_system_prompt()
        self._apply_permission_mode_prompt()

        # Initialize clients
        if self.use_openai:
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            self._anthropic_client = anthropic.AsyncAnthropic(**kwargs)
            self._openai_client = None

    def _apply_permission_mode_prompt(self) -> None:
        """按当前 permission_mode 设 _plan_file_path + _system_prompt（__init__ 与 rebind_session 共用）。
        plan 模式：_system_prompt = base + plan 提示（文本内嵌 plan-<sid>.md 路径）；否则 = base。"""
        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt

    def _build_tracer(self, *, trace_enabled: bool, trace_parent):
        """构造本 agent 的 Tracer：始终带一个 always-on 的 per-agent wire sink，
        外加可选的 debug 轨迹 sink（--trace / NANOCODE_TRACE，或父携带的 debug sink）。

        - wire sink 永远独立：写 agent_dir(session, artifact_id)/wire.jsonl，绝不与父
          复用（否则所有子 agent 事件会并进父文件）。
        - debug sink：子继承父的 _debug_sinks（父若开了 debug trace，子共写
          ./.nanocode/traces/<parent_sid>.jsonl）；但 wire sink 是各自独立的。
        - parent_session_id：子 agent = 父 session_id；主 agent = NANOCODE_TRACE_PARENT env。
        - 失败保护：JsonlSink 已对 I/O 故障自禁用；wire 路径解析若抛错也吞掉、退化为无 wire
          sink，绝不让 __init__ 失败。
        """
        # 1) per-agent wire sink（always-on，独立文件）
        sinks: list = []
        start_seq = 0
        try:
            wire_path = _session_v2.agent_wire_path(self.session_id, self.artifact_id)
            # resume-safe：从既有 wire tail 续 seq，避免 evt_{agent_id}_{seq} 跨运行碰撞。
            from ..events.reader import next_seq_from_wire
            start_seq = next_seq_from_wire(wire_path)
            sinks.append(JsonlSink(wire_path))
        except Exception:
            pass  # 仪表化绝不影响 agent 启动

        # 2) 可选 debug sink
        debug_sinks: list = []
        if trace_parent is not None:
            # 子 agent：继承父的 debug sink（若有），不继承父的 wire sink。
            debug_sinks = list(getattr(trace_parent, "_debug_sinks", []) or [])
            parent_session_id = trace_parent.session_id
        else:
            # 主 agent：trace_enabled 或 NANOCODE_TRACE 开启时附加 ./.nanocode/traces sink。
            if _trace_is_enabled(trace_enabled):
                try:
                    debug_sinks = list(build_default_sinks(self.session_id))
                except Exception:
                    debug_sinks = []
            parent_session_id = os.environ.get("NANOCODE_TRACE_PARENT", "").strip() or None

        tracer = Tracer(self.session_id, [*sinks, *debug_sinks],
                        parent_session_id=parent_session_id,
                        agent_id=self.artifact_id, start_seq=start_seq,
                        trajectory_enabled=self.trajectory_enabled,
                        trajectory_level=self.trajectory_level)
        # 标记 debug sink，供子 agent 继承（区别于 per-agent wire sink）。
        tracer._debug_sinks = debug_sinks
        return tracer

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
        # Lazily connect to MCP servers on first chat (main agent only)
        if not self._mcp_initialized and not self.is_sub_agent:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except Exception as e:
                self._sink.info(f"[mcp] Init failed: {e}")

        self._aborted = False
        self.tracer.begin_turn()  # 一次用户输入 = 一个 turn；后续事件携带该 turn_id
        self.tracer.emit("user_message", text=user_message)
        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self._current_task = asyncio.current_task()
        try:
            await coro
        except asyncio.CancelledError:
            self._aborted = True
        finally:
            self._current_task = None
        self.tracer.emit(
            "turn_end", input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens, turns=self.current_turns,
        )
        if not self.is_sub_agent:
            self._auto_save()

    # ─── Sub-agent entry point ────────────────────────────────

    async def run_once(self, prompt: str) -> dict:
        # 每轮入口重置捕获——复刻旧 `_output_buffer = []`，使复用的（持久/resume/headless）
        # 子 agent 实例不把上一轮文本泄漏进本轮结果（Codex review P2）。
        resetter = getattr(self._sink, "reset", None)
        if callable(resetter):
            resetter()
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        try:
            await self.chat(prompt)
        finally:
            # always-on wire sink：无论成功/异常/取消都 emit session_end + 关闭句柄，
            # 否则错误/超时/取消路径会泄漏每个子 agent 的 wire.jsonl 文件句柄。
            self.tracer.emit(
                "session_end", input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens, turns=self.current_turns,
            )
            self.tracer.close()
        return {
            "text": self._captured_text(),
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out,
            },
        }

    # ─── Output helper ────────────────────────────────────────

    def _captured_text(self) -> str:
        """子 agent 累积的助手文本：从注入的 BufferSink 取回（无 buffer 能力则空串）。"""
        getter = getattr(self._sink, "text", None)
        return getter() if callable(getter) else ""

    @staticmethod
    def _subagent_captured_text(sub_agent) -> str:
        """父读取子 agent 已捕获的 partial 文本（超时/错误终态用）——经子的 sink，
        不再 reach 进已删除的 _output_buffer 字段。"""
        if sub_agent is None:
            return ""
        return sub_agent._captured_text()

    def _emit_block(self, text: str) -> None:
        self._dispatch_event("assistant_block", text=text)

    # ─── REPL commands ────────────────────────────────────────

    def clear_history(self) -> None:
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self._sent_skill_names = set()
        self._pending_skill_bodies = []
        self._activated_path_skills = set()
        self._active_hooks = []
        reset_skill_cache()
        self._sink.info("Conversation cleared.")

    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        self._sink.info(f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    def _get_current_cost_usd(self) -> float:
        return (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15

    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    async def compact(self) -> None:
        await self._compact_conversation()

    # ─── Message-list ownership（docs/13 S5：plain list，MessageStore 抽象已删）──────
    # _anthropic_messages / _openai_messages 是普通 list（每轮 build_context 投影 + 注入装饰）；
    # 树是事实源。跨 agent：父读子经 _dump_messages、装入经 _load_messages（不直接赋子列表）。

    def _active_messages(self) -> list:
        return self._openai_messages if self.use_openai else self._anthropic_messages

    def _load_messages(self, messages: list) -> None:
        """装入活动列表（resume / move_to / 父恢复子 agent 单一入口）。"""
        if self.use_openai:
            self._openai_messages = messages
        else:
            self._anthropic_messages = messages

    def _replace_messages(self, messages: list) -> None:
        self._load_messages(messages)

    def _append_message(self, message) -> None:
        self._active_messages().append(message)

    def _dump_messages(self) -> list:
        """dump：导出活动列表（持久化只读；含父读子 agent 列表）。"""
        return list(self._active_messages())

    # ─── Session ──────────────────────────────────────────────

    def restore_session(self, data: dict) -> None:
        """docs/14 §4.2：canonical session.jsonl 树是 resume 首选权威。

        无树 → 从盘上 legacy 自动迁移建树（migration.migrate_session，best-effort）。树**非空** → 从树
        重建（_build_request_messages 后续也从树渲染）；树空/缺 → 回退 data 的 flat 列表（此时
        _build_request_messages 在 build_context 空时也回退 flat，二者一致）。
        注：tree 写入是 best-effort（_tree_record guarded）；§10#5 接受舍弃旧 no-data-loss 保证，
        但 _tree_record 失败已可观测（见其实现）；P7 删 legacy。"""
        from ..session.manager import SessionManager
        from ..session.render import ModelCtx, render
        if not SessionManager.exists(self.session_id):
            try:
                from ..session.migration import migrate_session
                migrate_session(self.session_id, model=self.model)
            except Exception:
                pass
        legacy = (data.get("openaiMessages") if self.use_openai else data.get("anthropicMessages")) or []
        tree_msgs = None
        if SessionManager.exists(self.session_id):
            self._session_mgr = SessionManager.open(self.session_id)   # 续写仍走树
            built = self._session_mgr.build_context()
            if built.messages:
                api = "openai-completions" if self.use_openai else "anthropic"
                sysp = self._system_prompt if self.use_openai else None
                tree_msgs = render(built.messages,
                                   ModelCtx(provider=self._current_provider(), api=api, model_id=self.model),
                                   system_prompt=sysp)["messages"]
        if tree_msgs:
            self._load_messages(tree_msgs)        # 非空树 = 权威
        elif legacy:
            self._load_messages(legacy)           # 树空/缺 → legacy 兜底（build_context 空时请求也走 flat）
        # v2 state: load TaskManager + mark non-terminal entries as lost
        self._reload_task_state(data.get("state"))
        self._sink.info(f"Session restored ({self._get_message_count()} messages).")

    def _reload_task_state(self, state) -> None:
        """load 一个 session 的 v2 state 进 task_manager，并把非终态 task/subagent 标 "lost"
        （进程已不在跑它们）。restore_session 与 rebind_session（docs/14 P2）共用。state 缺/非 dict → no-op。"""
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

    def _finalize_tracer(self) -> None:
        """emit session_end + close 当前 tracer（flush 缓冲、释放 wire.jsonl 句柄）。guarded。"""
        try:
            self.tracer.emit("session_end", input_tokens=self.total_input_tokens,
                             output_tokens=self.total_output_tokens, turns=self.current_turns)
            self.tracer.close()
        except Exception:
            pass

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
        self._context_cleared = False
        self._apply_permission_mode_prompt()    # 按 baseline mode 重算 _plan_file_path + _system_prompt（新 sid）

    def rebind_session(self, new_sid: str, *, artifact_id: str = "main",
                       parent_session: dict | None = None) -> None:
        """原地把**主** agent 重指到另一个 session：finalize 旧 session 的全部 session-keyed 状态，
        再 rebuild 新 session 的。复用同一 Agent 实例（保留 MCP/memory/clients/tools/system_prompt/
        审批回调），只换 session 维度——使 /new /resume /clone /fork 与子父导航共用一条原子替换路径
        （docs/14 §0/§3.3）。fail-closed 前置（turn/后台/子 agent 运行中拒绝）由 RuntimeHost.can_switch
        在调用前保证。new_sid==当前 sid → no-op（resume 到当前会话不折腾）。"""
        if self.is_sub_agent:
            raise RuntimeError("rebind_session is for the main agent only")
        if new_sid == self.session_id:
            return
        old_sid = self.session_id
        from ..session.manager import SessionManager
        from ..session.render import ModelCtx, render
        # ── Pre-flight：先取**新** session 写锁 + open/create + build_context。任何失败（含
        #    SessionBusyError：另一进程在写该 session）都在 finalize 旧 session **之前**抛出——保证
        #    rebind 原子（codex B1）且 fail-closed 不让第二个 writer 进同一 session（docs/14 §6a）。
        #    build_context 抛错（torn/cyclic 树）时必须释放刚拿的新锁，否则同进程重试 /resume 自锁死
        #    （P6 review #1/#6）。
        new_mgr = (SessionManager.open(new_sid, lock=True) if SessionManager.exists(new_sid)
                   else SessionManager.create(new_sid, parent_session=parent_session, lock=True))
        try:
            built = new_mgr.build_context()
        except BaseException:
            new_mgr.close()                     # 释放刚取的新锁，避免泄漏/自锁死
            raise
        # ── FINALIZE 旧 session（pre-flight 已过，下面均为低风险/guarded 操作）──
        old_mgr = self._session_mgr
        self._finalize_tracer()                 # session_end + close（释放旧 wire 句柄）
        self._auto_save()                       # 旧 session 的 legacy snapshot + v2 state
        if old_mgr is not None:
            old_mgr.close()                     # 释放旧 session 写锁
        try:
            from ..tools.sandbox_shell import cleanup_persist_sandbox
            cleanup_persist_sandbox(old_sid)    # 旧 persist sandbox + fingerprint
        except Exception:
            pass
        # ── REBUILD 新 session ──
        # _session_mgr 立即指向 new_mgr：即便后续 rebuild 步骤抛错，新锁也由 agent 持有引用、不泄漏，
        # 且 agent 不会停在引用已 close 的旧 mgr 的撕裂态（P6 review #2）。
        self.session_id = new_sid
        self.artifact_id = artifact_id
        os.environ["NANOCODE_SESSION_ID"] = new_sid
        self._session_mgr = new_mgr             # pre-flight 已 open/create + 持锁
        self.tracer = self._build_tracer(trace_enabled=self._trace_enabled, trace_parent=None)
        self.tracer.emit("session_start", model=self.model, cwd=str(Path.cwd()),
                         permission_mode=self.permission_mode, is_sub_agent=False,
                         workspace_trusted=self.workspace_trusted)
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
        # 活动消息列表：从新 session 树（pre-flight 的 built）render 装入（空 session → []，openai 含 system）
        provider = "openai" if self.use_openai else "anthropic"
        api = "openai-completions" if self.use_openai else "anthropic"
        sysp = self._system_prompt if self.use_openai else None
        self._load_messages(render(built.messages,
                                   ModelCtx(provider=provider, api=api, model_id=self.model),
                                   system_prompt=sysp)["messages"])
        self._sink.info(f"Session → {new_sid} ({self._get_message_count()} messages).")

    def _get_message_count(self) -> int:
        return len(self._openai_messages) if self.use_openai else len(self._anthropic_messages)

    def _auto_save(self) -> None:
        # docs/14 P7：canonical session.jsonl 树是 resume 权威——不再写 legacy flat <sid>.json 快照
        # （之前由 store.save_session 写，现冗余）。仍按需落 v2 state.json（TaskManager/subagent 派生 cache）。
        # v2 state: persist when session has forked subagents, background tasks, or is already v2
        # （含 list_tasks：仅有后台 shell 任务、无 subagent 的 session 也要落 state，否则 /resume
        #  回来时丢任务记录——docs/14 P2 review）。
        if (_session_v2.is_v2_session(self.session_id) or self.task_manager.list_subagents()
                or self.task_manager.list_tasks()):
            self._persist_state()

    def _tree_record(self, provider_msg: dict, *, stop_reason: "str | None" = None) -> None:
        """docs/13 cutover S1 + docs/14 full-P6b：把一条 live provider 消息以**干净原文**写进 canonical
        session.jsonl 树（_tree_session_id：主 agent=自身 session，子 agent=独立 child session）。
        message-end 调用；注入是 render-time 装饰、单独以 custom_message 入树。全 guarded：绝不破坏 live turn。

        stop_reason：assistant 消息记录 backend 的**真实** provider stop/finish reason（docs/14 §4.3
        bug#2，忠实而非内容推断）；此处按 provider 映射成中立值再交 capture。"""
        try:
            from ..session import capture
            from ..session.manager import SessionManager
            if self._session_mgr is None:
                tsid = self._tree_session_id
                self._session_mgr = (SessionManager.open(tsid) if SessionManager.exists(tsid)
                                     else SessionManager.create(tsid, parent_session=self._child_parent_session))
            provider = "openai" if self.use_openai else "anthropic"
            neutral_sr = capture.neutral_stop_reason(provider, stop_reason)
            cap = capture.capture_openai if self.use_openai else capture.capture_anthropic
            for neutral in cap(provider_msg, model=self.model, stop_reason=neutral_sr):
                self._session_mgr.append_message(neutral)
        except Exception as e:
            # tree 写入是 best-effort（§10#5 接受），但失败必须**可观测**——否则 tree-only resume 会
            # 静默丢这条消息（docs/14 P3 review #3）。emit 一条 tracer 事件供审计/告警。
            try:
                self.tracer.emit("tree_record_failed", role=provider_msg.get("role"), error=str(e))
            except Exception:
                pass

    def _build_request_messages(self) -> list:
        """docs/13 cutover S2：从 canonical 树渲染本轮请求（`render(build_context())`）。

        树是会话事实源（含 S1 message-end 写入的消息 + P5 注入的 custom_message）；render 据当前
        provider 整形 + 合并相邻 user（复刻注入的 append-to-last-user 定位）。无树时回退扁平列表
        （首轮 / 未启用）。Anthropic system 走 out-of-band，OpenAI system 经 render 注入 index 0。"""
        flat = self._openai_messages if self.use_openai else self._anthropic_messages
        if self._session_mgr is None:
            return flat
        try:
            from ..session import tree as _tree
            from ..session.render import ModelCtx, render
            provider = "openai" if self.use_openai else "anthropic"
            api = "openai-completions" if self.use_openai else "anthropic"
            sysp = self._system_prompt if self.use_openai else None
            # 树须含真实 MESSAGE（user/assistant/toolResult）才算权威——只有 header + custom_message
            # 注入时（如 user 消息 _tree_record 失败但注入成功）回退 flat，否则会把真实 user 消息挤掉、
            # 只发注入文本给模型（docs/14 P3 review #8）。
            if not any(e.type == _tree.MESSAGE for e in self._session_mgr.get_branch()):
                return flat
            built = self._session_mgr.build_context()
            if not built.messages:
                return flat
            return render(built.messages, ModelCtx(provider=provider, api=api, model_id=self.model),
                          system_prompt=sysp)["messages"]
        except Exception:
            return flat

    def _persist_state(self) -> None:
        """Write v2 state (tasks + subagents) to disk —— DERIVED cache（非 resume 权威，docs/14 P7）。
        canonical 树是会话事实源；这里只落 TaskManager/subagent 生命周期记录供 /resume 重载 + mark-lost。"""
        try:
            state = self.task_manager.to_state()
            state["session_id"] = self.session_id
            state["startTime"] = self.session_start_time
            _session_v2.write_state(self.session_id, state)
        except Exception:
            pass

    # ─── Autocompact ──────────────────────────────────────────

    async def _check_and_compact(self) -> None:
        if self.last_input_token_count > self.effective_window * 0.85:
            self._sink.info("Context window filling up, compacting conversation...")
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        before = self._get_message_count()
        tokens_before = self.last_input_token_count
        # bug#1（docs/14 §4.4 + P3 review #5）：kept-tail 起点必须与 backend 实际保留的对齐。
        # backend 仅当末条消息是 user 时才把它接到 summary 之后（_compact_*: last_user_msg.role=='user'），
        # 否则 summary 之后不留任何旧消息。auto-compact 触发于刚记完 user 消息时（leaf==该 user）→
        # firstKept=该 user；manual /compact 在 turn 间（leaf 是 assistant/tool）→ firstKept=None（旧消息
        # 全被 summary 顶替，不复现）。故 cut = last_user id 仅当它==leaf，否则 None。
        first_kept = None
        if self._session_mgr is not None:
            leaf = self._session_mgr.get_leaf()
            last_u = self._session_mgr.last_user_message_id()
            first_kept = last_u if last_u == leaf else None
        if self.use_openai:
            summary = await self._compact_openai()
        else:
            summary = await self._compact_anthropic()
        # S4（docs/13）：compaction-as-entry —— additive 写一条 compaction 树 entry（summary +
        # firstKeptEntryId），供 build_context 两区 fold。**主 agent 与 full-P6b 子 agent 都写**（子写自己
        # 的 child 树）——否则子的 _build_request_messages 从未压缩的 child 树重渲染会抵消压缩（review high）。
        if summary:
            try:
                from ..session.manager import SessionManager
                if self._session_mgr is None:
                    self._session_mgr = (SessionManager.open(self._tree_session_id)
                                         if SessionManager.exists(self._tree_session_id)
                                         else SessionManager.create(self._tree_session_id,
                                                                    parent_session=self._child_parent_session))
                    leaf = self._session_mgr.get_leaf()
                    last_u = self._session_mgr.last_user_message_id()
                    first_kept = last_u if last_u == leaf else None
                self._session_mgr.append_compaction(
                    summary=summary, tokens_before=tokens_before,
                    first_kept_entry_id=first_kept)
            except Exception:
                pass
        # 保留事件名 compaction（report.py 硬读它），additive 补压缩前后消息数——供 /tree 与审计。
        # 注：rebuild 经 llm_request 快照 oracle 已忠实反映 post-compaction 状态，无需 supersession 重放。
        self.tracer.emit("compaction", kind="auto",
                         message_count_before=before, message_count_after=self._get_message_count())
        self._sink.info("Conversation compacted.")
        self._sent_skill_names = set()  # 清单消息被压缩丢弃 → 下一轮重新播报

    # ─── Skill progressive disclosure ─────────────────────────

    def _skill_listing_budget(self) -> int:
        return max(2000, int(self.effective_window * 0.04))

    def _tree_custom_message(self, custom_type: str, content, *, parent_id: "str | None" = None) -> bool:
        """docs/13 P5 / docs/14 §4.5+full-P6b：把一次注入作为 custom_message entry 写进 canonical 树
        （主 agent=自身树，子 agent=child 树；按 _session_mgr 而非 is_sub_agent gate）。parent_id 显式给定
        时挂到指定 entry（background pin-to-spawn-branch 用）。返回是否真正写入——调用方据此推进 dedup /
        flat 兜底（docs/14 P3 review #7：写失败不得静默丢注入 + 不得误推进 dedup）。"""
        if self._session_mgr is None:
            return False
        try:
            from ..session import tree as _tree
            self._session_mgr.append(_tree.CUSTOM_MESSAGE,
                                     {"customType": custom_type, "content": content, "display": False},
                                     parent_id=parent_id)
            return True
        except Exception:
            return False

    def _inject_skill_listing(self, messages: list) -> None:
        if self.is_sub_agent:
            return
        text, new_names = skill_listing_delta(
            self._sent_skill_names, self._activated_path_skills, self._skill_listing_budget()
        )
        if text:
            # docs/14 §4.5：有树（主 agent 常态）→ 写 custom_message（请求由 _build_request_messages 从树
            # 渲染）；无树 → flat 注入（此时 flat 是请求源）。dedup（_sent_skill_names）**只在注入真正生效后**
            # 推进——有树但树写失败时，flat 兜底会被 _build_request_messages 丢弃，故不推进 dedup、下一轮重试，
            # 不静默丢清单（review medium：原来无条件推进会永久丢失）。
            if self._session_mgr is not None:
                if self._tree_custom_message("skill_listing", text):
                    self._sent_skill_names.update(new_names)
                # 树写失败：不推进 dedup（下一轮重试树写）；不走 flat（会被树渲染丢弃）
            else:
                append_to_last_user(messages, text)        # 无树 → flat 即请求源，生效
                self._sent_skill_names.update(new_names)

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

    def _inject_pending_skill_bodies(self, messages: list) -> None:
        # 有树（主 agent 或 full-P6b 子 agent）→ custom_message；无树 → flat 注入。
        tree_backed = self._session_mgr is not None
        for name, body in self._pending_skill_bodies:
            msg = render_skill_body_message(name, body)
            if not (tree_backed and self._tree_custom_message("skill_body", msg.get("content", ""))):
                messages.append(msg)
        self._pending_skill_bodies = []

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
            self.task_manager, rec.id, command, stdout_path, stderr_path, timeout_ms))
        task._nanocode_task_id = rec.id
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return rec.id

    def _inject_finished_tasks(self, messages: list) -> None:
        """turn boundary 注入：终态且未注入的后台任务渲染成 <system-reminder>。主 agent（有树）→
        custom_message entry 挂在 **live leaf**（必须在当前 branch 上，否则模型看不到完成提醒——这
        优先于 docs/14 §6b 的"pin 到 spawn 分支"：字面 parent_id=spawn_leaf 会造成 sibling 分支、
        既不可见又会 fork 掉用户后续 turn）。spawn 血缘记在 task.spawn_entry_id（state.json 持久）供审计。
        无树（子 agent 早期）→ flat 追加到 last user message。dedup 经 task.injected 持久标记。"""
        if self.is_sub_agent:
            return   # 子 agent 与父共享 TaskManager；finished-task 回注是**父**（user-facing loop）的职责，
                     # 否则子会"偷走"并标 injected 父/兄弟的后台完成提醒，使父永不浮现（review high）。
        pending = collect_pending_injections(self.task_manager)
        if not pending:
            return
        text = "\n\n".join(render_task_reminder(t) for t in pending)
        wrote = (self._session_mgr is not None
                 and self._tree_custom_message("finished_tasks", text))
        if not wrote:                          # 无树 / 树写失败 → flat 兜底（追加到 last user message）
            last = messages[-1] if messages else None
            if last and last.get("role") == "user":
                content = last.get("content", "")
                if isinstance(content, str):
                    last["content"] = content + "\n\n" + text
                elif isinstance(content, list):
                    content.append({"type": "text", "text": text})
                else:
                    messages.append({"role": "user", "content": text})
            else:
                messages.append({"role": "user", "content": text})
        # 注入已落地（树或 flat）才标 injected——避免树写失败时 dedup 误推进、丢提醒（docs/14 P3 review #7）。
        for t in pending:
            self.task_manager.update_task(t.id, injected=True)

    def _tool_blocked_by_allowlist(self, name: str) -> bool:
        """P4 call-time allowlist 判定——委托给 PermissionEngine（单一决策来源）。

        语义见 tools.permissions.allowlist_blocks。保留本薄包装供 callgate
        (_execute_tool_call) 与 hook-shell 路径 (_run_hook) 调用，二者即 fail-closed 兜底点。
        """
        return self.permission.allowlist_blocks(name)

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        # P4 call-time allowlist enforcement（安全基石）：在任何真实工具派发（含
        # run_shell 后台分支）之前 fail-closed。仅约束 REAL 工具——子 agent 合法持有的
        # meta 工具（task_*/memory/plan_mode/skill；agent 永远被剥）不在约束内。
        # 这是覆盖前台 + 后台 run_shell 的单一咽喉点：run_shell 后台分支在下方先于 meta
        # 拦截返回，若不在此处先判，read-only agent 仍能借 run_in_background 跑 run_shell。
        if self._tool_blocked_by_allowlist(name):
            self.tracer.emit("tool_blocked", tool=name, reason="not_in_allowlist",
                             agent_type=self.agent_type, artifact_id=self.artifact_id)
            return f"Error: tool '{name}' is not permitted for this sub-agent."
        if name == "run_shell" and inp.get("run_in_background"):
            tid = await self._spawn_background_shell(inp.get("command", ""), inp.get("timeout"))
            return (f"Started background shell task {tid}. It will report completion later. "
                    f"Use task_output with task_id={tid} to inspect progress.")
        if name == "task_list":
            return tasks_tool.list_tasks_text(self.task_manager, inp.get("status"), inp.get("kind"))
        if name == "task_output":
            return tasks_tool.task_output_text(self.task_manager, inp.get("task_id", ""),
                                               int(inp.get("tail_bytes") or 8000))
        if name == "task_stop":
            # 子 agent 共享父 TaskManager：限制其只能 stop 自己持有协程的 task，
            # 不得把父/兄弟的 task 标 cancelled（orphan-cancel 仅主 agent 可用）。
            return await tasks_tool.task_stop(
                self.task_manager, self._background_tasks, inp.get("task_id", ""),
                allow_orphan_cancel=not self.is_sub_agent)
        if name == "memory" and inp.get("action") == "recall" and inp.get("semantic"):
            return await self._recall_memory_semantic(inp.get("query", ""), int(inp.get("limit") or 5))
        if name == "memory" and inp.get("action") == "consolidate":
            # 记忆巩固会 spawn 一个 curator（孙 agent）并批量改写宿主记忆文件——属
            # 宿主/会话级操作，子 agent 不得触发（否则绕过 agent 后备 + depth/threads）。
            if self.is_sub_agent:
                return ("Error: memory consolidation is a host/session operation and "
                        "is not available to sub-agents.")
            return await self._spawn_memory_consolidate()
        if name in ("enter_plan_mode", "exit_plan_mode"):
            # Plan mode 是主 agent / REPL 流程，会改写 self.permission_mode。子 agent
            # 若能 exit_plan_mode 就能把自己从 plan 放宽到 default——自我提权。禁用之。
            if self.is_sub_agent:
                return "Error: plan-mode tools are not available to sub-agents."
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        # 真实工具(mcp/execute_tool)——受 hooks 约束(meta 工具上面已返回)
        # session_id 单一注入点：sandbox/run_shell 走显式 _session_id（去全局竞态），保留 env 回退
        if name in ("run_shell", "sandbox_shell") and "_session_id" not in inp:
            inp = {**inp, "_session_id": self.session_id}
        if self._suppress_hooks or not self._active_hooks:
            return await self._run_real_tool(name, inp)
        for h in self._matching_hooks("pre-tool-use", name):
            ok, msg = await self._run_hook(h, name, inp, None)
            if not ok:
                return f"[blocked by skill hook {h['skill']} (pre-tool-use)] {msg}"
        result = await self._run_real_tool(name, inp)
        warnings = []
        for h in self._matching_hooks("post-tool-use", name):
            ok, msg = await self._run_hook(h, name, inp, result)
            if not ok:
                warnings.append(f"[skill hook {h['skill']} (post-tool-use) warning] {msg}")
        if warnings:
            result = result + "\n\n" + "\n".join(warnings)
        return result

    async def _run_real_tool(self, name: str, inp: dict) -> str:
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        result = await execute_tool(name, inp, self._read_file_state)
        if name in ("read_file", "write_file", "edit_file") and not result.startswith(("Error", "Warning")):
            self._on_file_touched(name, inp)
        return result

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
            self.tracer.emit("tool_blocked", tool="run_shell", reason="hook_not_in_allowlist",
                             agent_type=self.agent_type, artifact_id=self.artifact_id)
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
        hook_inp = {"command": cmd, "timeout": h["timeout_ms"], "stdin": json.dumps(event)}
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

    # ─── Sub-agent factory (centralized permission inheritance) ──

    def _parent_remaining_turns(self) -> int | None:
        """父若有 max_turns 预算，返回剩余可用 turn 数（>=0），否则 None（无界）。"""
        if self.max_turns is None:
            return None
        return max(0, self.max_turns - self.current_turns)

    # ─── P4 concurrency / depth caps（策略已抽入 SubAgentManager，CAP-P1；以下为委托 shim）─────

    def _running_background_subagent_count(self) -> int:
        return self._subagents.running_background_count()

    def _depth_cap_exceeded(self) -> bool:
        return self._subagents.depth_cap_exceeded()

    def _max_threads(self) -> int:
        return self._subagents.max_threads()

    def _background_subagent_cap_reached(self) -> bool:
        return self._subagents.background_cap_reached()

    @staticmethod
    def _foreground_timeout(tool_timeout_ms, config: dict, fleet_cfg: dict):
        return SubAgentManager.foreground_timeout(tool_timeout_ms, config, fleet_cfg)

    def _bounded_sub_agent_max_turns(self, manifest_max_turns: int | None) -> int:
        return self._subagents.bounded_max_turns(manifest_max_turns)

    def _build_sub_agent(self, *, system_prompt, tools, agent_type, session_id=None,
                         background=False, max_turns=None, model=None,
                         artifact_id=None, agent_source=None) -> "Agent":
        """构造子 agent：集中权限继承。

        与 Claude Code / Kimi Code 对齐：
        - 子继承父 permission_mode（硬规则「子不得高于父」，不再无条件 bypass）。
        - 共享父 confirm_fn + _confirmed_paths（确认回流到父，同一引用）。
        - 共享 session_id + task_manager；trace 父子靠 trace_parent 对象。
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
        """
        safe_tools = [t for t in tools if t.get("name") != "agent"]
        confirm_fn = _auto_deny_confirm if background else self.confirm_fn
        confirmed_paths = set() if background else self._confirmed_paths
        allowed_tool_names = {t["name"] for t in safe_tools}
        sub = Agent(
            model=model or self.model,
            api_base=str(self._openai_client.base_url) if self.use_openai and self._openai_client else None,
            custom_system_prompt=system_prompt,
            custom_tools=safe_tools,
            is_sub_agent=True,
            permission_mode=self.permission_mode,
            confirm_fn=confirm_fn,
            confirmed_paths=confirmed_paths,
            session_id=session_id or self.session_id,
            task_manager=self.task_manager,
            trace_parent=self.tracer,
            # 子 agent 继承 trajectory 采集开关；trajectory_id 不显式传，由子 Tracer 从
            # session_id 派生（traj_<session_id>）。父子共享同一 session_id（上面
            # session_id=session_id or self.session_id，无调用方传子 session_id），故父子
            # trajectory_id 必然一致——这是个隐式不变量：若将来给子 agent 独立 session_id，
            # 需改为显式透传 trajectory_id，否则父子 trajectory_id 会悄悄分叉。
            trajectory_enabled=self.trajectory_enabled,
            trajectory_level=self.trajectory_level,
            max_turns=max_turns,
            artifact_id=artifact_id,
            allowed_tool_names=allowed_tool_names,
            depth=self.depth + 1,
            agent_type=agent_type,
            agent_source=agent_source,
        )
        # docs/14 full-P6b：子 agent 把 transcript 写进独立 child session.jsonl（child sid 由 artifact_id
        # 派生；session_id/artifacts/trajectory 仍 parent-keyed，故 1173-1188 trajectory 不变量保持）。
        if artifact_id and artifact_id != "main":
            sub._tree_session_id = self.child_session_id(artifact_id)
            sub._child_parent_session = {"sessionId": self.session_id,
                                         "entryId": self._subagent_spawn_leaf.get(artifact_id),
                                         "taskId": artifact_id, "agentId": artifact_id}
        return sub

    # ─── Skill fork mode ─────────────────────────────────────

    async def _execute_skill_tool(self, inp: dict) -> str:
        from ..skills import execute_skill, get_skill_by_name
        sk = get_skill_by_name(inp.get("skill_name", ""))
        if sk and sk.disable_model_invocation:
            return f'Skill "{inp.get("skill_name", "")}" cannot be invoked by the model (disable-model-invocation).'
        if sk and getattr(sk, "hooks", None):
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
            if self._depth_cap_exceeded():
                return ("Error: max sub-agent depth reached; skill fork not spawned.")
            tools = (
                [t for t in self.tools if t["name"] in result["allowed_tools"]]
                if result.get("allowed_tools")
                else [t for t in self.tools if t["name"] != "agent"]
            )
            skill_name = inp.get("skill_name", "")
            fork_prompt = inp.get("args") or "Execute this skill task."
            # 每次 fork 注册独立 SubAgentRecord → 各自的 artifact_id/dir/wire，
            # 避免多次 skill-fork 把事件并入同一个 agents/skill-fork/wire.jsonl。
            rec = self.task_manager.create_subagent(
                type="skill-fork", description=skill_name,
                model=self.model, provider=self._current_provider(),
            )
            self.task_manager.update_subagent(rec.id, status="running")
            self._write_agent_spawn_artifacts(
                agent_id=rec.id, agent_type="skill-fork", description=skill_name,
                prompt=fork_prompt, model=self.model, background=False)
            self._sink.sub_agent_start("skill-fork", skill_name)
            sub_agent = None
            try:
                sub_agent = self._build_sub_agent(
                    system_prompt=result["prompt"],
                    tools=tools,
                    agent_type="coder",
                    max_turns=self._bounded_sub_agent_max_turns(None),
                    artifact_id=rec.id,
                )
                # 经 _await_subagent_run（与前台一致）而非裸 await run_once：
                # chat() 会吞掉 CancelledError，裸 await 会把真实取消误当成功。
                # 此处无 wall-clock 超时，kind=='timeout' 即表示被取消/abort。
                kind, payload = await self._await_subagent_run(sub_agent, fork_prompt, None)
            except asyncio.CancelledError:
                self.task_manager.update_subagent(rec.id, status="cancelled")
                if sub_agent is not None:
                    self._persist_agent_messages(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "cancelled")
                self._sink.sub_agent_end("skill-fork", skill_name)
                raise
            except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
                self.task_manager.update_subagent(rec.id, status="failed")
                if sub_agent is not None:
                    self._persist_agent_messages(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "failed")
                self._sink.sub_agent_end("skill-fork", skill_name)
                return f"Skill fork error: {e}"

            if kind == "timeout":
                # 无超时设定 → 'timeout' 表示运行被取消/aborted：落 cancelled 并向上传播取消。
                self.task_manager.update_subagent(rec.id, status="cancelled")
                self._persist_agent_messages(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "cancelled")
                self._sink.sub_agent_end("skill-fork", skill_name)
                raise asyncio.CancelledError()
            if kind == "error":
                self.task_manager.update_subagent(rec.id, status="failed")
                self._persist_agent_messages(rec.id, sub_agent)
                self._finalize_agent_meta(rec.id, "failed")
                self._sink.sub_agent_end("skill-fork", skill_name)
                return f"Skill fork error: {payload}"

            sub_result = payload
            self.total_input_tokens += sub_result["tokens"]["input"]
            self.total_output_tokens += sub_result["tokens"]["output"]
            self.task_manager.update_subagent(rec.id, status="completed")
            self._persist_agent_messages(rec.id, sub_agent)
            result_path = self._write_agent_result(rec.id, sub_result["text"] or "")
            self._finalize_agent_meta(rec.id, "completed")
            self._sink.sub_agent_end("skill-fork", skill_name)
            # 与 fresh/resume 一致：回传有界信封而非整段 transcript（完整在 result.md）。
            return self._finalize_foreground_result(sub_agent, sub_result, result_path, rec.id)

        self._pending_skill_bodies.append((inp.get("skill_name", ""), result["prompt"]))
        return f'[skill "{inp.get("skill_name", "")}" loaded — its instructions follow in the next message]'

    def _current_provider(self) -> str:
        return "openai" if self.use_openai else "anthropic"

    def _persist_agent_messages(self, agent_id: str, sub_agent: "Agent") -> None:
        """Persist sub-agent messages to v2 session storage (parent-keyed artifacts；back-compat)。"""
        try:
            # 经子 agent owner 的 dump 入口读，不再直接 reach 进 sub_agent._{provider}_messages。
            msgs = sub_agent._dump_messages()
            _session_v2.write_agent_messages(self.session_id, agent_id, msgs)
        except Exception:
            pass
        # full-P6b：子 agent transcript 已由其自身 _tree_record 实时写进 child session.jsonl（不再需要
        # finalize 镜像）。close 子的 child SessionManager，释放句柄/锁，使 /agent 导航后续可 lock 进入。
        try:
            if sub_agent._session_mgr is not None:
                sub_agent._session_mgr.close()
        except Exception:
            pass

    def child_session_id(self, agent_id: str) -> str:
        """子 agent 的 child session id（docs/14 §6b）。父 sid 作前缀，保证跨父唯一。"""
        return f"{self.session_id}.{agent_id}"

    def _write_agent_spawn_artifacts(self, *, agent_id: str, agent_type: str,
                                     description: str, prompt: str, model: str,
                                     background: bool) -> None:
        """子 agent 创建时落 prompt.txt + meta.json(status=running)。失败绝不影响主流程。"""
        # docs/14 §6b：记下 spawn 时父 leaf，供 finalize 镜像 child session 时 pin parentSession.entryId。
        try:
            self._subagent_spawn_leaf[agent_id] = (self._session_mgr.get_leaf()
                                                   if self._session_mgr is not None else None)
        except Exception:
            self._subagent_spawn_leaf[agent_id] = None
        try:
            _session_v2.write_agent_prompt(self.session_id, agent_id, prompt or "")
        except Exception:
            pass
        try:
            _session_v2.write_agent_meta(self.session_id, agent_id, {
                "id": agent_id,
                "type": agent_type,
                "description": description,
                "model": model,
                "provider": self._current_provider(),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "background": background,
                "parent_session_id": self.session_id,
                "status": "running",
            })
        except Exception:
            pass

    def _finalize_agent_meta(self, agent_id: str, status: str) -> None:
        """子 agent 终态时补 status + ended_at（合并已有 meta.json）。失败绝不影响主流程。"""
        try:
            meta = _session_v2.read_agent_meta(self.session_id, agent_id) or {"id": agent_id}
            meta["status"] = status
            meta["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _session_v2.write_agent_meta(self.session_id, agent_id, meta)
        except Exception:
            pass

    def _write_agent_result(self, agent_id: str, text: str) -> str | None:
        """把子 agent 最终文本写到 <agent_dir>/result.md，返回路径（失败返回 None）。"""
        try:
            return _session_v2.write_agent_result(self.session_id, agent_id, text or "")
        except Exception:
            return None

    # ─── Structured AgentResult + bounded envelope ────────────────
    # 纯函数（无 self / IO / 模型循环）已抽入 agent_result.py（CAP-P1 STEP 1）；以下为委托 shim。

    def _build_agent_result(self, sub_agent: "Agent", text: str,
                            tokens: dict, result_path: str | None) -> dict:
        return agent_result.build_agent_result(sub_agent, text, tokens, result_path)

    def _render_agent_result_envelope(self, result: dict, raw_text: str) -> str:
        return agent_result.render_agent_result_envelope(result, raw_text)

    def _dispatch_event(self, _type: str, **fields) -> None:
        """RUNTIME-P1：单流发射。durable → tracer（写 wire），UI → projection（读 live
        self.tracer / self._sink）。过渡期逐类把 self._sink.* + tracer.emit(...) 收敛到此；
        wire 与 UI 行为与双发逐字等价（byte-parity gate）。"""
        runtime_events.dispatch_event(
            runtime_events.RuntimeEvent(_type, fields), self.tracer, self._sink)

    def _fold_subagent_tokens(self, sub_agent: "Agent") -> None:
        """把子 agent 已花费的 token 折叠进父——成功/超时/错误都要折，否则一个跑了很久
        才超时/出错的子 agent 对 max_cost_usd 完全不可见（runaway 成本黑洞）。子 agent
        是全新实例（从 0 起算），故其 total_*_tokens 即为本次增量。"""
        try:
            self.total_input_tokens += getattr(sub_agent, "total_input_tokens", 0) or 0
            self.total_output_tokens += getattr(sub_agent, "total_output_tokens", 0) or 0
        except Exception:
            pass

    def _write_terminal_result(self, agent_id: str, sub_agent, reason: str) -> str | None:
        """终态（超时/错误）写 result.md：有 partial 输出就写它，否则写 reason。
        返回路径，供 task.result_path/last_result_path——让 reminder 指向 agent_dir
        而非渲染误导性的空 shell 日志。"""
        partial = self._subagent_captured_text(sub_agent)
        return self._write_agent_result(agent_id, partial or reason)

    def _finalize_foreground_terminal(self, sub_agent: "Agent", record_id: str,
                                      kind: str, payload, timeout_ms: int | None) -> str:
        """前台 timeout/error 终态共用：折叠 token（成本可见）+ 落 partial result.md +
        回传带宿主派生 files_modified 的最小信封（而非裸 '[timed out]' 字符串），
        使「改了文件后超时」的子 agent 也给父留下面包屑。"""
        self._fold_subagent_tokens(sub_agent)
        partial = self._subagent_captured_text(sub_agent)
        reason = (f"[sub-agent timed out after {timeout_ms} ms]" if kind == "timeout"
                  else str(payload))
        result_path = self._write_agent_result(record_id, partial or reason)
        if result_path:
            try:
                self.task_manager.update_subagent(record_id, last_result_path=result_path)
            except Exception:
                pass
        agent_result = self._build_agent_result(
            sub_agent, partial or reason, {"input": 0, "output": 0}, result_path)
        agent_result["summary"] = reason + (
            " — partial transcript persisted" if partial else "")
        return self._render_agent_result_envelope(agent_result, "")

    def _finalize_foreground_result(self, sub_agent: "Agent", result: dict,
                                    result_path: str | None, record_id: str | None) -> str:
        """前台/skill-fork 成功路径共用：装配 AgentResult → 渲染有界信封 → 回填
        SubAgentRecord.last_result_path。返回给父的就是这个有界信封（非整段 transcript）。"""
        text = result.get("text") or ""
        agent_result = self._build_agent_result(
            sub_agent, text, result.get("tokens") or {}, result_path)
        if record_id is not None and result_path:
            try:
                self.task_manager.update_subagent(record_id, last_result_path=result_path)
            except Exception:
                pass
        return self._render_agent_result_envelope(agent_result, text)

    # ─── Background sub-agent (detached, auto-deny-but-continue) ──

    def _write_subagent_result(self, task_id: str, text: str) -> str | None:
        """把子 agent 完整输出写到 task_dir/result.md，返回路径（失败返回 None）。"""
        try:
            d = _session_v2.task_dir(self.session_id, task_id)
            p = d / "result.md"
            p.write_text(text or "", encoding="utf-8")
            return str(p)
        except Exception:
            return None

    async def _spawn_background_subagent(self, *, agent_type: str, description: str,
                                         prompt: str, timeout_ms: int | None = None) -> str:
        """注册 subagent + task（双向链）+ detached 协程，立即返回 task_id。"""
        # 记录 EFFECTIVE 模型（manifest 覆盖优先），与前台/resume 一致。
        eff_model = get_sub_agent_config(agent_type).get("model") or self.model
        sub_rec = self.task_manager.create_subagent(
            type=agent_type, description=description,
            model=eff_model, provider=self._current_provider(),
        )
        self.task_manager.update_subagent(sub_rec.id, status="running")
        task_rec = self.task_manager.create_task(
            "subagent", description, owner_agent_id=sub_rec.id)
        self.task_manager.update_subagent(sub_rec.id, task_id=task_rec.id)
        # docs/14 §6b：记下 spawn 时父 leaf，供完成回注 pin 到 spawn 分支（而非完成时的 live leaf）。
        try:
            self.task_manager.update_task(
                task_rec.id, spawn_entry_id=(self._session_mgr.get_leaf() if self._session_mgr else None))
        except Exception:
            pass
        self._write_agent_spawn_artifacts(
            agent_id=sub_rec.id, agent_type=agent_type, description=description,
            prompt=prompt, model=eff_model, background=True)
        self._sink.sub_agent_start(agent_type, description)
        task = asyncio.create_task(self._run_background_subagent(
            agent_id=sub_rec.id, task_id=task_rec.id, agent_type=agent_type,
            description=description, prompt=prompt, timeout_ms=timeout_ms))
        task._nanocode_task_id = task_rec.id
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task_rec.id

    async def _run_background_subagent(self, *, agent_id: str, task_id: str, agent_type: str,
                                       description: str, prompt: str,
                                       timeout_ms: int | None) -> None:
        """detached 协程：构造 background 子 agent，跑 run_once，落终态 + 持久化。"""
        sub_agent = None
        try:
            # 注意：构造放进 try——cancel 可能在协程首个 await 之前送达
            # （status 已在 _spawn_* 同步置 running），此时仍须走 cancelled 清理。
            config = get_sub_agent_config(agent_type)
            sub_agent = self._build_sub_agent(
                system_prompt=config["system_prompt"],
                tools=config["tools"],
                agent_type=agent_type,
                background=True,
                max_turns=self._bounded_sub_agent_max_turns(config.get("max_turns")),
                model=config.get("model"),
                artifact_id=agent_id,
                agent_source=config.get("source"),
            )
            # 复用与前台一致的可靠超时原语（_await_subagent_run 内部不依赖 wait_for，
            # 因此不受 chat() 吞 CancelledError 影响——超时不会被误判为完成）。
            kind, payload = await self._await_subagent_run(sub_agent, prompt, timeout_ms)
        except asyncio.CancelledError:
            self.task_manager.update_task(
                task_id, status="cancelled",
                result_summary="(cancelled by task_stop)")
            self.task_manager.update_subagent(agent_id, status="cancelled")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "cancelled")
            self._sink.sub_agent_end(agent_type, description)
            raise
        except Exception as e:  # noqa: BLE001 — 构造/启动期异常也须落终态，detached 任务不能悬挂 running
            self.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(sub-agent error: {e})")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "failed")
            self._sink.sub_agent_end(agent_type, description)
            return

        if kind == "timeout":
            # SUBAGENT_STATUSES 现含 timed_out；保留既有约定：task=timed_out, sub=failed。
            self._fold_subagent_tokens(sub_agent)  # 成本可见：超时也折算 token
            rp = self._write_terminal_result(agent_id, sub_agent,
                                             f"(timed out after {timeout_ms}ms)")
            self.task_manager.update_task(
                task_id, status="timed_out", result_path=rp,
                result_summary=f"(timed out after {timeout_ms}ms)")
            self.task_manager.update_subagent(agent_id, status="failed",
                                              last_result_path=rp)
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "timed_out")
            self._sink.sub_agent_end(agent_type, description)
            return
        if kind == "error":
            self._fold_subagent_tokens(sub_agent)  # 成本可见：出错也折算 token
            rp = self._write_terminal_result(agent_id, sub_agent,
                                             f"(sub-agent error: {payload})")
            self.task_manager.update_task(
                task_id, status="failed", error=str(payload), result_path=rp,
                result_summary=f"(sub-agent error: {payload})")
            self.task_manager.update_subagent(agent_id, status="failed",
                                              last_result_path=rp)
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "failed")
            self._sink.sub_agent_end(agent_type, description)
            return

        result = payload  # kind == "ok"
        # 成功：token 累加进父 + result.md + result_summary + 持久化 messages
        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        # result.md 双写：task_dir（既有，供 task_output）+ agent_dir（本 agent 自包含）。
        result_path = self._write_subagent_result(task_id, text)
        agent_result_path = self._write_agent_result(agent_id, text)
        # P3：result_summary 用结构化 AgentResult 的 summary（模型自述或宿主回退），
        # result_path 指向 task_dir/result.md，last_result_path 指向 agent_dir/result.md。
        agent_result = self._build_agent_result(
            sub_agent, text, result["tokens"], result_path)
        self.task_manager.update_task(
            task_id, status="completed", result_path=result_path,
            result_summary=agent_result["summary"])
        self.task_manager.update_subagent(
            agent_id, status="completed", last_result_path=agent_result_path)
        self._persist_agent_messages(agent_id, sub_agent)
        self._finalize_agent_meta(agent_id, "completed")
        self._sink.sub_agent_end(agent_type, description)

    # ─── Memory consolidation (Auto-Dream) ────────────────────

    async def _spawn_memory_consolidate(self) -> str:
        """触发记忆巩固：curator 子 agent 出 JSON 提案 → 宿主 parse+apply。

        无记忆短路：build_curator_user_message() 返回 "No memory files..." 时不建
        task/subagent，直接返回提示。否则注册 subagent(memory-curator) + task
        (memory_consolidate, owner=agent_id) + detached _run_memory_consolidate，
        立即返回 task_id 提示（同 _spawn_background_subagent 的双向链 + 后台登记）。
        """
        user_message = build_curator_user_message()
        if user_message.startswith("No memory files"):
            return "No memories to consolidate."
        if self._background_subagent_cap_reached():
            return (f"Error: max concurrent sub-agents ({self._max_threads()}) reached; "
                    f"memory consolidation not started — try again later.")

        description = "memory consolidation"
        sub_rec = self.task_manager.create_subagent(
            type=self._MEMORY_CURATOR_TYPE, description=description,
            model=self.model, provider=self._current_provider(),
        )
        self.task_manager.update_subagent(sub_rec.id, status="running")
        task_rec = self.task_manager.create_task(
            "memory_consolidate", description, owner_agent_id=sub_rec.id)
        self.task_manager.update_subagent(sub_rec.id, task_id=task_rec.id)
        self._write_agent_spawn_artifacts(
            agent_id=sub_rec.id, agent_type=self._MEMORY_CURATOR_TYPE,
            description=description, prompt=user_message, model=self.model,
            background=True)
        self._sink.sub_agent_start(self._MEMORY_CURATOR_TYPE, description)
        task = asyncio.create_task(self._run_memory_consolidate(
            agent_id=sub_rec.id, task_id=task_rec.id, user_message=user_message))
        task._nanocode_task_id = task_rec.id
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return (f"Started memory consolidation task {task_rec.id}. It will report completion later. "
                f"Use task_output with task_id={task_rec.id} to inspect the proposal + result.")

    async def _run_memory_consolidate(self, *, agent_id: str, task_id: str,
                                      user_message: str, timeout_ms: int | None = None) -> None:
        """detached 协程：判断型(curator)+确定性(Python apply)解耦。

        **不复用** _run_background_subagent（后者把子文本当最终 result；巩固需 parse+apply
        后处理）。**绕开** _execute_agent_tool（其 type 归一会把 memory-curator 改成 coder
        拿全工具）——直接 get_sub_agent_config(memory-curator)[tools=[]] + _build_sub_agent
        (background=True)。四态对称：cancel/timeout/error 写终态；成功则 token 累加 + 持久化
        messages + 写 result.md，再 parse(坏JSON→completed "no changes")+apply→summary_line。
        """
        sub_agent = None
        description = "memory consolidation"
        try:
            config = get_sub_agent_config(self._MEMORY_CURATOR_TYPE)
            sub_agent = self._build_sub_agent(
                system_prompt=config["system_prompt"],
                tools=config["tools"],
                agent_type=self._MEMORY_CURATOR_TYPE,
                background=True,
                max_turns=self._bounded_sub_agent_max_turns(config.get("max_turns")),
                artifact_id=agent_id,
            )
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    sub_agent.run_once(user_message), timeout=timeout_ms / 1000.0)
            else:
                result = await sub_agent.run_once(user_message)
        except asyncio.CancelledError:
            self.task_manager.update_task(
                task_id, status="cancelled",
                result_summary="(cancelled by task_stop)")
            self.task_manager.update_subagent(agent_id, status="cancelled")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "cancelled")
            self._sink.sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            raise
        except asyncio.TimeoutError:
            self.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "timed_out")
            self._sink.sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            return
        except Exception as e:
            self.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(curator error: {e})")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "failed")
            self._sink.sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            return

        # curator 成功产出 JSON 提案：token 累加 + 持久化 + 写 result.md
        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = self._write_subagent_result(task_id, text)
        self._write_agent_result(agent_id, text)
        self.task_manager.update_subagent(agent_id, status="completed")
        self._persist_agent_messages(agent_id, sub_agent)
        self._finalize_agent_meta(agent_id, "completed")

        # 确定性 parse+apply（宿主 Python，可回滚）。坏 JSON 不让 task failed，标 completed。
        try:
            plan = parse_consolidation_plan(text)
        except Exception:
            self.task_manager.update_task(
                task_id, status="completed", result_path=result_path,
                result_summary="Consolidation: no changes (unparseable plan)")
            self._sink.sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            return

        apply_result = apply_plan(plan)
        self.task_manager.update_task(
            task_id, status="completed", result_path=result_path,
            result_summary=apply_result.summary_line())
        self._sink.sub_agent_end(self._MEMORY_CURATOR_TYPE, description)

    # ─── Memory eval candidate generation (EVAL-mode curator) ──

    async def _spawn_memory_eval(self) -> str:
        """触发 eval 候选生成：EVAL-mode curator 子 agent 出候选 JSON →
        宿主逐条 add_pending（非法跳过）。无记忆短路。"""
        user_message = build_eval_curator_message()
        if user_message.startswith("No memory files"):
            return "No memories to generate eval candidates from."
        if self._background_subagent_cap_reached():
            return (f"Error: max concurrent sub-agents ({self._max_threads()}) reached; "
                    f"memory eval not started — try again later.")
        # eval 候选 provenance 的 source.session_id 必须指向真实存在的 session，
        # 否则 add_pending 校验会拒掉全部候选。REPL 命令不走 chat()，在此显式落盘。
        self._persist_state()

        description = "memory eval generation"
        sub_rec = self.task_manager.create_subagent(
            type=self._MEMORY_EVAL_CURATOR_TYPE, description=description,
            model=self.model, provider=self._current_provider(),
        )
        self.task_manager.update_subagent(sub_rec.id, status="running")
        task_rec = self.task_manager.create_task(
            "memory_eval", description, owner_agent_id=sub_rec.id)
        self.task_manager.update_subagent(sub_rec.id, task_id=task_rec.id)
        self._write_agent_spawn_artifacts(
            agent_id=sub_rec.id, agent_type=self._MEMORY_EVAL_CURATOR_TYPE,
            description=description, prompt=user_message, model=self.model,
            background=True)
        self._sink.sub_agent_start(self._MEMORY_EVAL_CURATOR_TYPE, description)
        task = asyncio.create_task(self._run_memory_eval(
            agent_id=sub_rec.id, task_id=task_rec.id, user_message=user_message))
        task._nanocode_task_id = task_rec.id
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return (f"Started memory eval generation task {task_rec.id}. It will report completion later. "
                f"Use task_output with task_id={task_rec.id} to inspect generated candidates.")

    async def _run_memory_eval(self, *, agent_id: str, task_id: str,
                               user_message: str, timeout_ms: int | None = None) -> None:
        """detached 协程：curator 出候选 JSON → 宿主逐条 eval_store.add_pending。

        宿主强制 source.session_id = self.session_id（不信任 curator）。校验失败的
        候选计入 skipped，不让 task failed。坏 JSON → completed 0 candidates。"""
        from ..memory import eval_store
        sub_agent = None
        description = "memory eval generation"
        try:
            config = get_sub_agent_config(self._MEMORY_EVAL_CURATOR_TYPE)
            sub_agent = self._build_sub_agent(
                system_prompt=config["system_prompt"],
                tools=config["tools"],
                agent_type=self._MEMORY_EVAL_CURATOR_TYPE,
                background=True,
                max_turns=self._bounded_sub_agent_max_turns(config.get("max_turns")),
                artifact_id=agent_id,
            )
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    sub_agent.run_once(user_message), timeout=timeout_ms / 1000.0)
            else:
                result = await sub_agent.run_once(user_message)
        except asyncio.CancelledError:
            self.task_manager.update_task(
                task_id, status="cancelled",
                result_summary="(cancelled by task_stop)")
            self.task_manager.update_subagent(agent_id, status="cancelled")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "cancelled")
            self._sink.sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
            raise
        except asyncio.TimeoutError:
            self.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "timed_out")
            self._sink.sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
            return
        except Exception as e:
            self.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(eval curator error: {e})")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            self._finalize_agent_meta(agent_id, "failed")
            self._sink.sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
            return

        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = self._write_subagent_result(task_id, text)
        self._write_agent_result(agent_id, text)
        self.task_manager.update_subagent(agent_id, status="completed")
        self._persist_agent_messages(agent_id, sub_agent)
        self._finalize_agent_meta(agent_id, "completed")

        # 确定性后处理：解析候选并逐条 add_pending（坏 JSON / 缺 candidates → 0）。
        added = 0
        skipped = 0
        try:
            from ..memory.maintenance import extract_json_object
            data = json.loads(extract_json_object(text))
            candidates = data.get("candidates", []) if isinstance(data, dict) else []
        except Exception:
            self.task_manager.update_task(
                task_id, status="completed", result_path=result_path,
                result_summary="Generated 0 pending eval candidates (unparseable output)")
            self._sink.sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
            return

        for item in candidates:
            if not isinstance(item, dict):
                skipped += 1
                continue
            source = dict(item.get("source") or {})
            source["session_id"] = self.session_id  # 宿主统一填 provenance
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
        self.task_manager.update_task(
            task_id, status="completed", result_path=result_path,
            result_summary=summary)
        self._sink.sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)

    # ─── Memory optimization (EvolveMem, host-only) ───────────

    async def _spawn_memory_optimize(self) -> str:
        """触发记忆检索配置优化：prune confirmed evals → 阈值门控 →
        simplemem.optimize → 原子落 evolve_config.json。纯宿主计算（无 curator）。

        与 consolidate/eval 不同：**不**注册 subagent（optimize 非判断型任务），
        仅建 task（kind=memory_optimize, owner=None）+ detached _run_memory_optimize。
        也**不**短路：即便 backend 不可用也建 task，让 task 报告 unavailable，
        这样 REPL 用户能 task_output 看到有意义的诊断结果。
        """
        description = "memory optimization"
        task_rec = self.task_manager.create_task(
            "memory_optimize", description, owner_agent_id=None)
        task = asyncio.create_task(self._run_memory_optimize(task_id=task_rec.id))
        task._nanocode_task_id = task_rec.id
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return (f"Started memory optimization task {task_rec.id}. It will report completion later. "
                f"Use task_output with task_id={task_rec.id} to inspect the result.")

    async def _run_memory_optimize(self, *, task_id: str, timeout_ms: int | None = None) -> None:
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

        backend = self._memory_backend
        try:
            # ① backend duck-type 判定（不 import SimpleMemBackend，避免顶层耦合）
            mem = getattr(backend, "_system", None)
            if getattr(backend, "name", "") != "simplemem" or mem is None:
                self.task_manager.update_task(
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
                self.task_manager.update_task(
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
            self.task_manager.update_task(
                task_id, status="completed",
                result_summary=(f"memory_optimize: evolved config saved "
                                f"({len(dev)} dev questions, pruned {pruned}, "
                                f"rounds {rounds}) -> {path}"))
        except asyncio.CancelledError:
            self.task_manager.update_task(
                task_id, status="cancelled",
                result_summary="(cancelled by task_stop)")
            raise
        except asyncio.TimeoutError:
            self.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            return
        except Exception as e:
            self.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(optimize error: {e})")
            return

    async def _await_subagent_run(self, sub_agent: "Agent", prompt: str,
                                  timeout_ms: int | None) -> tuple[str, object]:
        """可靠地 await 一次子 agent run_once，并施加可选 wall-clock 超时。

        返回 (kind, payload)：'ok'->result dict；'timeout'->None；'error'->Exception。

        关键：Agent.chat() 会吞掉 CancelledError（优雅 abort），因此 asyncio.wait_for
        在超时 cancel 后内层吞掉取消、"正常返回"——wait_for 会回值而非抛 TimeoutError。
        这里改用 asyncio.wait 的 pending 集合可靠判定超时，并用 _aborted 兜底。
        外层取消（如 task_stop）先取消内层任务再向上传播，避免任务泄漏。
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

    async def _run_foreground_subagent(self, sub_agent: "Agent", prompt: str,
                                       timeout_ms: int | None,
                                       record_id: str | None) -> tuple[str, str | dict]:
        """前台子 agent 执行：施加 wall-clock 超时，永不让异常逃逸。

        返回 (kind, payload)：
        - kind='ok'      -> payload 是 run_once 的 result dict（含 text + tokens）。
        - kind='timeout' -> payload 是给模型的超时字符串；record 已标 'timed_out'。
        - kind='error'   -> payload 是给模型的错误字符串。
        """
        kind, payload = await self._await_subagent_run(sub_agent, prompt, timeout_ms)
        if kind == "timeout":
            if record_id is not None:
                try:
                    self.task_manager.update_subagent(record_id, status="timed_out")
                except Exception:
                    pass
            return "timeout", f"[sub-agent timed out after {timeout_ms} ms]"
        if kind == "error":
            return "error", f"Sub-agent error: {payload}"
        return "ok", payload  # type: ignore[return-value]

    async def _execute_agent_tool(self, inp: dict) -> str:
        # ─── Variable extraction (hoisted; shared by background/resume/fresh) ───
        agent_type = inp.get("type", "general")
        # Type normalization: general/coder synonym; known custom types pass through;
        # truly-unknown → coder. RESERVED types are never spawnable via the tool.
        from ..subagents.config import _discover_custom_agents, RESERVED_AGENT_TYPES
        if agent_type in ("general", "coder"):
            agent_type = "coder"
        elif agent_type in ("explore", "plan"):
            pass
        elif agent_type in _discover_custom_agents() and agent_type not in RESERVED_AGENT_TYPES:
            pass  # 已发现的自定义类型：保留，按其 manifest 解析配置
        else:
            agent_type = "coder"  # 真正未知（含保留名）→ general 语义
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        resume_id = inp.get("resume")
        # 工具入参 timeout_ms 优先；缺省时回退 manifest 'timeout-ms'，再回退 settings
        # [agents] default/background_timeout_ms（item 2/4）。
        tool_timeout_ms = inp.get("timeout_ms")
        from ..tools import load_agents_config
        fleet_cfg = load_agents_config()

        # ─── P4 max_depth backstop（纵深防御，适用于所有 spawn 路径）───
        # 子不能 spawn 孙（agent 工具被剥），故今天 live depth 恒为 1；此为前瞻性 backstop。
        if self._depth_cap_exceeded():
            return (f"Error: max sub-agent depth ({fleet_cfg.get('max_depth')}) reached; "
                    f"cannot spawn a sub-agent at depth {self.depth + 1}.")

        # ─── run_in_background: detached subagent (auto-deny-but-continue) ───
        if inp.get("run_in_background"):
            if resume_id:
                return "Error: run_in_background cannot be combined with resume."
            # P4 max_threads：cap 并发运行的后台子 agent。前台子 agent 阻塞父、天然串行，
            # 故 cap 只施于后台 spawn——超限直接拒绝、绝不 spawn（fail-closed）。
            max_threads = self._max_threads()
            if max_threads > 0 and self._running_background_subagent_count() >= max_threads:
                return (f"Error: max concurrent sub-agents ({max_threads}) reached; "
                        f"try again later.")
            bg_cfg = get_sub_agent_config(agent_type)
            bg_timeout = tool_timeout_ms
            if bg_timeout is None:
                bg_timeout = bg_cfg.get("timeout_ms")
            if bg_timeout is None:
                bg_timeout = fleet_cfg.get("background_timeout_ms")
            task_id = await self._spawn_background_subagent(
                agent_type=agent_type, description=description, prompt=prompt,
                timeout_ms=bg_timeout)
            return (f"Started background sub-agent task {task_id}. It will report completion later. "
                    f"Use task_output with task_id={task_id} to inspect progress.")

        # ─── resume path ─────────────────────────────────────────
        if resume_id:
            rec = self.task_manager.get_subagent(resume_id)
            if not rec:
                return f"Error: sub-agent '{resume_id}' not found (unknown id)."
            # 保留类型（curator 等宿主内部 agent）不可经公开 agent 工具 resume —
            # 否则已知 agent-NNN curator 记录会被任意 prompt 重跑（结合空工具集回退提权）。
            if rec.type in RESERVED_AGENT_TYPES:
                return (f"Error: sub-agent '{resume_id}' is a reserved internal agent "
                        f"and cannot be resumed via the agent tool.")
            # Provider mismatch check
            if rec.provider and rec.provider != self._current_provider():
                return (f"Error: provider mismatch — sub-agent '{resume_id}' was created with "
                        f"provider '{rec.provider}' but current provider is '{self._current_provider()}'. "
                        f"Cannot resume across providers.")
            # Reload history and build sub-agent
            config = get_sub_agent_config(rec.type)
            # 当前有效模型（manifest 覆盖优先）；与 record 里存的 EFFECTIVE 模型比对。
            current_eff_model = config.get("model") or self.model
            # Model mismatch check（基于有效模型，而非父模型）
            if rec.model and rec.model != current_eff_model:
                return (f"Error: model mismatch — sub-agent '{resume_id}' was created with "
                        f"model '{rec.model}' but its current effective model is '{current_eff_model}'. "
                        f"Cannot resume with a different model.")
            eff_timeout = self._foreground_timeout(tool_timeout_ms, config, fleet_cfg)
            max_turns = self._bounded_sub_agent_max_turns(config.get("max_turns"))
            self._sink.sub_agent_start(rec.type, description)
            # Update record status
            self.task_manager.update_subagent(resume_id, status="running")
            self._write_agent_spawn_artifacts(
                agent_id=resume_id, agent_type=rec.type, description=description,
                prompt=prompt, model=rec.model or current_eff_model, background=False)

            sub_agent = None
            try:
                sub_agent = self._build_sub_agent(
                    system_prompt=config["system_prompt"],
                    tools=config["tools"],
                    agent_type=rec.type,
                    max_turns=max_turns,
                    model=rec.model or current_eff_model,
                    artifact_id=resume_id,
                    agent_source=config.get("source"),
                )
                # docs/14 P7-a：subagent resume 从其 **child session.jsonl** 重建（full-P6b 起子 agent
                # 实时写 child 树），退役 SessionContextBuilder 的 wire/snapshot 重建。child 树缺（pre-
                # full-P6b 旧子 agent）→ 把 v2 snapshot **seed 进 child 树**（不能只装 flat list——子的
                # 首个 _tree_record 会建一棵只含新 prompt 的 child 树，_build_request_messages 从树渲染
                # 会丢掉装入的 flat 历史，导致旧上下文全失，review high）。
                from ..session import capture
                from ..session.manager import SessionManager
                from ..session.render import ModelCtx, render
                child_sid = self.child_session_id(resume_id)
                if not SessionManager.exists(child_sid):
                    legacy = _session_v2.read_agent_messages(self.session_id, resume_id)
                    if legacy:
                        cmgr = SessionManager.create(child_sid, parent_session=sub_agent._child_parent_session)
                        for n in capture.capture_provider_messages(legacy, self._current_provider(),
                                                                   model=sub_agent.model):
                            cmgr.append_message(n)
                        cmgr.close()
                if SessionManager.exists(child_sid):
                    built = SessionManager.open(child_sid).build_context()
                    api = "openai-completions" if self.use_openai else "anthropic"
                    sysp = sub_agent._system_prompt if self.use_openai else None
                    history = render(built.messages,
                                     ModelCtx(provider=self._current_provider(), api=api, model_id=sub_agent.model),
                                     system_prompt=sysp)["messages"]
                else:
                    history = []
                sub_agent._load_messages(history)

                kind, payload = await self._run_foreground_subagent(
                    sub_agent, prompt, eff_timeout, resume_id)
            except asyncio.CancelledError:
                self.task_manager.update_subagent(resume_id, status="cancelled")
                if sub_agent is not None:
                    self._persist_agent_messages(resume_id, sub_agent)
                self._finalize_agent_meta(resume_id, "cancelled")
                self._sink.sub_agent_end(rec.type, description)
                raise
            except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
                self.task_manager.update_subagent(resume_id, status="failed")
                if sub_agent is not None:
                    self._persist_agent_messages(resume_id, sub_agent)
                self._finalize_agent_meta(resume_id, "failed")
                self._sink.sub_agent_end(rec.type, description)
                return f"Sub-agent error: {e}"
            if kind != "ok":
                # timeout：record 已在 helper 内标 'timed_out'；error：这里补标 failed。
                if kind == "error":
                    self.task_manager.update_subagent(resume_id, status="failed")
                self._persist_agent_messages(resume_id, sub_agent)
                self._finalize_agent_meta(
                    resume_id, "timed_out" if kind == "timeout" else "failed")
                self._sink.sub_agent_end(rec.type, description)
                return self._finalize_foreground_terminal(
                    sub_agent, resume_id, kind, payload, eff_timeout)
            result = payload  # type: ignore[assignment]
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            self.task_manager.update_subagent(resume_id, status="completed")
            self._persist_agent_messages(resume_id, sub_agent)
            result_path = self._write_agent_result(resume_id, result["text"] or "")
            self._finalize_agent_meta(resume_id, "completed")
            self._sink.sub_agent_end(rec.type, description)
            # P3：父收到的是有界信封（summary + findings + 宿主派生文件事实 + result_path），
            # 而非整段 transcript（transcript 已落 result.md，可经 read_file 取回）。
            return self._finalize_foreground_result(
                sub_agent, result, result_path, resume_id)

        # ─── fresh path ──────────────────────────────────────────
        config = get_sub_agent_config(agent_type)
        eff_timeout = self._foreground_timeout(tool_timeout_ms, config, fleet_cfg)
        max_turns = self._bounded_sub_agent_max_turns(config.get("max_turns"))
        eff_model = config.get("model") or self.model
        self._sink.sub_agent_start(agent_type, description)
        # Register SubAgentRecord — 记录 EFFECTIVE 模型（manifest 覆盖优先），
        # 否则 resume 的 model-mismatch 校验会拿父模型自比、形同虚设。
        rec = self.task_manager.create_subagent(
            type=agent_type, description=description,
            model=eff_model, provider=self._current_provider(),
        )
        self.task_manager.update_subagent(rec.id, status="running")
        self._write_agent_spawn_artifacts(
            agent_id=rec.id, agent_type=agent_type, description=description,
            prompt=prompt, model=eff_model, background=False)

        sub_agent = None
        try:
            sub_agent = self._build_sub_agent(
                system_prompt=config["system_prompt"],
                tools=config["tools"],
                agent_type=agent_type,
                max_turns=max_turns,
                model=eff_model,
                artifact_id=rec.id,
                agent_source=config.get("source"),
            )
            kind, payload = await self._run_foreground_subagent(
                sub_agent, prompt, eff_timeout, rec.id)
        except asyncio.CancelledError:
            # 用户中断：record/meta 必须落终态，不能悬挂 running。
            self.task_manager.update_subagent(rec.id, status="cancelled")
            if sub_agent is not None:
                self._persist_agent_messages(rec.id, sub_agent)
            self._finalize_agent_meta(rec.id, "cancelled")
            self._sink.sub_agent_end(agent_type, description)
            raise
        except Exception as e:  # noqa: BLE001 — 构造期异常也须落终态
            self.task_manager.update_subagent(rec.id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(rec.id, sub_agent)
            self._finalize_agent_meta(rec.id, "failed")
            self._sink.sub_agent_end(agent_type, description)
            return f"Sub-agent error: {e}"
        if kind != "ok":
            if kind == "error":
                self.task_manager.update_subagent(rec.id, status="failed")
            self._persist_agent_messages(rec.id, sub_agent)
            self._finalize_agent_meta(
                rec.id, "timed_out" if kind == "timeout" else "failed")
            self._sink.sub_agent_end(agent_type, description)
            return self._finalize_foreground_terminal(
                sub_agent, rec.id, kind, payload, eff_timeout)
        result = payload  # type: ignore[assignment]
        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        self.task_manager.update_subagent(rec.id, status="completed")
        self._persist_agent_messages(rec.id, sub_agent)
        result_path = self._write_agent_result(rec.id, result["text"] or "")
        self._finalize_agent_meta(rec.id, "completed")
        self._sink.sub_agent_end(agent_type, description)
        # P3：父收到的是有界信封（summary + findings + 宿主派生文件事实 + result_path），
        # 而非整段 transcript（transcript 已落 result.md，可经 read_file 取回）。
        return self._finalize_foreground_result(
            sub_agent, result, result_path, rec.id)

    # ─── Shared ──────────────────────────────────────────────────

    async def _authorize_dispatch(self, name: str, inp: dict) -> "tuple[bool, str | None]":
        """两后端派发前共用的授权 + 审批交互（单一入口，de-dup）。返回 ``(allowed, denial)``。

        - 经 ``self.permission.check`` 取 policy 决策并 emit ``permission_decision``；
        - ``deny`` → 打印并返回 ``"Action denied: …"``；
        - ``confirm`` → 走 ``_confirm_if_needed``（dedupe + 身份装饰）；拒则返回固定文案。

        **allowlist 不在此判**——它是 ``_execute_tool_call`` 的 fail-closed 兜底（保持子 agent
        拒绝消息 "Error: tool '…' is not permitted" 与 prompt-then-block 行为不变）。
        """
        d = self.permission.check(name, inp)
        self.tracer.emit("permission_decision", tool=name, action=d.action, message=d.message)
        if d.action == "deny":
            self._sink.info(f"Denied: {d.message}")
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
        self._sink.confirmation(message)
        if self.confirm_fn:
            return await self.confirm_fn(message)
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
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
