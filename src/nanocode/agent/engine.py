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
from .message_store import MessageStore
from .context_builder import SessionContextBuilder
from ..session import save_session
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
from .compaction import persist_large_result, CompressionPipeline
from .plan_mode import PlanModeMixin
from .anthropic_backend import AnthropicBackendMixin
from .openai_backend import OpenAIBackendMixin


# ─── Agent ───────────────────────────────────────────────────

# 前台子 agent 的回退 turn 上限：当 manifest 未声明 max-turns 时使用，
# 确保前台子 agent 永远有界（不至无限循环拖死父 loop）。
SUBAGENT_MAX_TURNS_FALLBACK = 50

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
        if not self.is_sub_agent:
            os.environ["NANOCODE_SESSION_ID"] = self.session_id
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

        # Separate message histories —— 各由一个 MessageStore owner 持有。
        # 经 _anthropic_messages / _openai_messages 属性访问（getter 返回 live list，
        # 读/索引/切片/append/in-place 裁剪零改动；整列赋值经 setter 路由到 owner）。
        self._anthropic_store = MessageStore()
        self._openai_store = MessageStore()

        # Build system prompt
        self._base_system_prompt = custom_system_prompt or build_system_prompt()
        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

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
                        agent_id=self.artifact_id, start_seq=start_seq)
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
        self._sink.assistant_markdown(text)

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

    # ─── Message-list ownership（P-1 子目标2：单一 owner 入口）──────
    # provider 消息列表由 MessageStore 持有。getter 暴露 live list（读/索引/切片/append/
    # in-place 裁剪零改动）；整列赋值经 setter 路由到 owner（resume/compaction/clear）。
    # 跨 agent 场景：父**不得**直接赋值子的列表——经 _load_messages / 读经 _dump_messages。

    @property
    def _openai_messages(self) -> list:
        return self._openai_store.items

    @_openai_messages.setter
    def _openai_messages(self, messages: list) -> None:
        self._openai_store.load(messages)

    @property
    def _anthropic_messages(self) -> list:
        return self._anthropic_store.items

    @_anthropic_messages.setter
    def _anthropic_messages(self, messages: list) -> None:
        self._anthropic_store.load(messages)

    def _active_store(self) -> MessageStore:
        return self._openai_store if self.use_openai else self._anthropic_store

    def _load_messages(self, messages: list) -> None:
        """load：用 history/快照接管本 agent 活动列表（resume 单一入口；含被父恢复的子 agent）。"""
        self._active_store().load(messages)

    def _replace_messages(self, messages: list) -> None:
        """replace：整列重置（compaction 摘要替换 / clear）。"""
        self._active_store().replace(messages)

    def _append_message(self, message) -> None:
        self._active_store().append(message)

    def _dump_messages(self) -> list:
        """dump：导出活动列表（持久化只读；含父读子 agent 列表）。"""
        return self._active_store().dump()

    # ─── Session ──────────────────────────────────────────────

    def restore_session(self, data: dict) -> None:
        if data.get("anthropicMessages"):
            self._anthropic_messages = data["anthropicMessages"]
        if data.get("openaiMessages"):
            self._openai_messages = data["openaiMessages"]
        # v2 state: load TaskManager + mark non-terminal entries as lost
        state = data.get("state")
        if state and isinstance(state, dict):
            self.task_manager.load_state(state)
            for t in self.task_manager.list_tasks():
                if t.status not in TERMINAL_TASK_STATUSES:
                    self.task_manager.update_task(t.id, status="lost")
            for a in self.task_manager.list_subagents():
                if a.status in ("running", "idle"):
                    self.task_manager.update_subagent(a.id, status="lost")
        self._sink.info(f"Session restored ({self._get_message_count()} messages).")

    def _get_message_count(self) -> int:
        return len(self._openai_messages) if self.use_openai else len(self._anthropic_messages)

    def _auto_save(self) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                },
                "anthropicMessages": self._anthropic_messages if not self.use_openai else None,
                "openaiMessages": self._openai_messages if self.use_openai else None,
            })
        except Exception:
            pass
        # v2 state: persist when session has forked subagents or is already v2
        if _session_v2.is_v2_session(self.session_id) or self.task_manager.list_subagents():
            self._persist_state()

    def _persist_state(self) -> None:
        """Write v2 state (tasks + subagents + main messages) to disk."""
        try:
            state = self.task_manager.to_state()
            state["session_id"] = self.session_id
            state["startTime"] = self.session_start_time
            _session_v2.write_state(self.session_id, state)
            _session_v2.write_main_messages(
                self.session_id,
                self._openai_messages if self.use_openai else self._anthropic_messages,
            )
        except Exception:
            pass

    # ─── Autocompact ──────────────────────────────────────────

    async def _check_and_compact(self) -> None:
        if self.last_input_token_count > self.effective_window * 0.85:
            self._sink.info("Context window filling up, compacting conversation...")
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        before = self._get_message_count()
        if self.use_openai:
            await self._compact_openai()
        else:
            await self._compact_anthropic()
        # 保留事件名 compaction（report.py 硬读它），additive 补压缩前后消息数——供 /tree 与审计。
        # 注：rebuild 经 llm_request 快照 oracle 已忠实反映 post-compaction 状态，无需 supersession 重放。
        self.tracer.emit("compaction", kind="auto",
                         message_count_before=before, message_count_after=self._get_message_count())
        self._sink.info("Conversation compacted.")
        self._sent_skill_names = set()  # 清单消息被压缩丢弃 → 下一轮重新播报

    # ─── Skill progressive disclosure ─────────────────────────

    def _skill_listing_budget(self) -> int:
        return max(2000, int(self.effective_window * 0.04))

    def _inject_skill_listing(self, messages: list) -> None:
        if self.is_sub_agent:
            return
        text, new_names = skill_listing_delta(
            self._sent_skill_names, self._activated_path_skills, self._skill_listing_budget()
        )
        if text:
            append_to_last_user(messages, text)
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
        for name, body in self._pending_skill_bodies:
            messages.append(render_skill_body_message(name, body))
        self._pending_skill_bodies = []

    # ─── Multi-tier compression pipeline ──────────────────────

    def _run_compression_pipeline(self) -> None:
        # 每次 API 调用前的 in-place 多层裁剪——委托给 CompressionPipeline facade
        # （tier 实现已从 backend 收敛到 compaction.py；此处行为不变，仍每轮跑）。
        if self.use_openai:
            CompressionPipeline.prepare_openai(
                self._openai_messages,
                last_input_token_count=self.last_input_token_count,
                effective_window=self.effective_window,
                last_api_call_time=self.last_api_call_time,
            )
        else:
            CompressionPipeline.prepare_anthropic(
                self._anthropic_messages,
                last_input_token_count=self.last_input_token_count,
                effective_window=self.effective_window,
                last_api_call_time=self.last_api_call_time,
            )

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
        """turn boundary 注入：终态且未注入的后台任务渲染成 <system-reminder> 追加到 last user message。"""
        pending = collect_pending_injections(self.task_manager)
        if not pending:
            return
        text = "\n\n".join(render_task_reminder(t) for t in pending)
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

    # ─── P4 concurrency / depth caps ─────────────────────────────

    def _running_background_subagent_count(self) -> int:
        """当前并发运行的后台子 agent 数：本 agent 持有的 detached task 中，回指一个
        SubAgentRecord（owner_agent_id 非空）且 status=='running' 的 TaskRecord 计数。

        以 owner_agent_id 而非 kind=='subagent' 判定，使 memory curator/eval/optimize
        这些同样跑后台子 agent 的任务也计入上限——否则它们会绕过 max_threads。
        shell 后台任务 owner_agent_id 为 None，不计。
        以 TaskManager 的权威状态为准（detached 协程结束落终态），不靠 task.done()。"""
        n = 0
        for t in self._background_tasks:
            tid = getattr(t, "_nanocode_task_id", None)
            if not tid:
                continue
            rec = self.task_manager.get_task(tid)
            if rec and rec.owner_agent_id and rec.status == "running":
                n += 1
        return n

    def _depth_cap_exceeded(self) -> bool:
        """新 spawn 的子 agent 深度（self.depth + 1）是否超过 max_depth。

        主 agent depth=0，其子 depth=1。今天子不能 spawn 孙（agent 工具被剥），故 live
        depth 结构上恒为 1；max_depth 是前瞻性纵深防御 backstop。"""
        from ..tools import load_agents_config
        max_depth = load_agents_config().get("max_depth")
        if not max_depth or max_depth <= 0:
            return False
        return (self.depth + 1) > max_depth

    def _max_threads(self) -> int:
        from ..tools import load_agents_config
        return load_agents_config().get("max_threads") or 0

    def _background_subagent_cap_reached(self) -> bool:
        """后台子 agent 是否已达 max_threads 上限。curator/eval 与 agent 工具共用此判定，
        使「计入」与「受限」一致——否则 curator 计入计数却不受限，自相矛盾。"""
        mt = self._max_threads()
        return mt > 0 and self._running_background_subagent_count() >= mt

    @staticmethod
    def _foreground_timeout(tool_timeout_ms, config: dict, fleet_cfg: dict):
        """前台子 agent 的有效超时：工具入参 > manifest 'timeout-ms' > settings
        [agents] default_timeout_ms（item 2/4）。全缺省 -> None（无 wall-clock 超时）。"""
        if tool_timeout_ms is not None:
            return tool_timeout_ms
        if config.get("timeout_ms") is not None:
            return config.get("timeout_ms")
        return fleet_cfg.get("default_timeout_ms")

    def _bounded_sub_agent_max_turns(self, manifest_max_turns: int | None) -> int:
        """计算前台子 agent 的 turn 上限：

        manifest max-turns 优先，否则回退 SUBAGENT_MAX_TURNS_FALLBACK；
        若父有剩余 turn 预算，clamp 到 min(value, parent_remaining)——子绝不超过父。
        """
        value = manifest_max_turns if (manifest_max_turns and manifest_max_turns > 0) else SUBAGENT_MAX_TURNS_FALLBACK
        remaining = self._parent_remaining_turns()
        if remaining is not None:
            value = min(value, remaining)
        return value

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
        return Agent(
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
            max_turns=max_turns,
            artifact_id=artifact_id,
            allowed_tool_names=allowed_tool_names,
            depth=self.depth + 1,
            agent_type=agent_type,
            agent_source=agent_source,
        )

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
        """Persist sub-agent messages to v2 session storage."""
        try:
            # 经子 agent owner 的 dump 入口读，不再直接 reach 进 sub_agent._{provider}_messages。
            msgs = sub_agent._dump_messages()
            _session_v2.write_agent_messages(self.session_id, agent_id, msgs)
        except Exception:
            pass

    def _write_agent_spawn_artifacts(self, *, agent_id: str, agent_type: str,
                                     description: str, prompt: str, model: str,
                                     background: bool) -> None:
        """子 agent 创建时落 prompt.txt + meta.json(status=running)。失败绝不影响主流程。"""
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

    # 小结果直通阈值：raw text <= 此字节数时整段作为 summary 直通父上下文
    # （concise explore/plan deliverable 不应被截断丢失）；超出则截断 + 指针。
    _ENVELOPE_PASSTHROUGH_BYTES = 4096
    _ENVELOPE_MAX_FINDINGS = 10
    _ENVELOPE_MAX_FILES = 10

    def _build_agent_result(self, sub_agent: "Agent", text: str,
                            tokens: dict, result_path: str | None) -> dict:
        """装配 AgentResult：宿主派生文件事实 + 模型自述 summary/findings（可选结构块解析，回退兜底）。

        files_read / files_modified 取自 SUB-AGENT 实例的观测集合（宿主派生，不信任模型）。
        summary / findings 由 parse_structured_result 解析子 agent 最终文本；无结构块则
        summary=首 ~500 字符、findings=[]。tokens 已折叠进父，仅展示。
        """
        from ..subagents.result import parse_structured_result
        parsed = parse_structured_result(text or "")
        files_read = sorted(getattr(sub_agent, "_files_read", None) or set())
        files_modified = sorted(getattr(sub_agent, "_files_modified", None) or set())
        return {
            "summary": parsed["summary"],
            "findings": parsed["findings"],
            "files_read": files_read,
            "files_modified": files_modified,
            "tokens": {"input": tokens.get("input", 0), "output": tokens.get("output", 0)},
            "result_path": result_path,
        }

    def _render_agent_result_envelope(self, result: dict, raw_text: str) -> str:
        """渲染**有界、定形**的信封——父上下文看到的就是这个（不再是整段 transcript）。

        规则：
        - raw_text 小（<= ~4KB）→ 整段直通作为 summary（concise deliverable 不丢失）；
          否则用模型 summary，并附 "... [truncated — full result at <path>, use read_file]" 指针。
        - 始终追加：top findings（cap ~10）、files_modified（cap ~10 名 + 溢出计数）、
          files_read 计数、tokens、result_path。
        - 无论 findings/files 多少都有界。
        """
        raw_text = raw_text or ""
        result_path = result.get("result_path")
        explicit_summary = (result.get("summary") or "").strip()
        small = len(raw_text.encode("utf-8")) <= self._ENVELOPE_PASSTHROUGH_BYTES
        if not raw_text.strip():
            # 空 transcript：若调用方已显式给了 summary（如超时/错误终态的原因），用它；
            # 否则给"无输出"提示。两种都仍带 result_path 指针。
            if explicit_summary:
                body = explicit_summary + (f"\nFull result at {result_path}, use read_file"
                                           if result_path else "")
            else:
                body = (f"(sub-agent produced no output; see {result_path})"
                        if result_path else "(sub-agent produced no output)")
        elif small:
            body = raw_text.strip()
        else:
            summary = explicit_summary or "(no summary)"
            pointer = (f"\n... [truncated — full result at {result_path}, use read_file]"
                       if result_path else
                       "\n... [truncated — full result not persisted]")
            body = summary + pointer

        lines = [body]

        findings = result.get("findings") or []
        if findings:
            shown = findings[:self._ENVELOPE_MAX_FINDINGS]
            lines.append("\nFindings:")
            lines.extend(f"  - {f}" for f in shown)
            if len(findings) > len(shown):
                lines.append(f"  - (+{len(findings) - len(shown)} more)")

        modified = result.get("files_modified") or []
        if modified:
            shown = modified[:self._ENVELOPE_MAX_FILES]
            lines.append("\nFiles modified:")
            lines.extend(f"  - {p}" for p in shown)
            if len(modified) > len(shown):
                lines.append(f"  - (+{len(modified) - len(shown)} more)")

        read_count = len(result.get("files_read") or [])
        tok = result.get("tokens") or {}
        lines.append(f"\nFiles read: {read_count}")
        lines.append(f"Tokens: {tok.get('input', 0)} in / {tok.get('output', 0)} out")
        lines.append(f"Result: {result_path or '(not persisted)'}")
        return "\n".join(lines)

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
                # Reload persisted messages —— 经 SessionContextBuilder 取上下文（P3 快照、
                # P5 事件树重建的统一入口），再经子 agent owner 入口加载（不直接赋值子列表）。
                history = SessionContextBuilder(self.session_id).resume_messages(agent_id=resume_id)
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
