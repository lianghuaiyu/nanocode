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
    check_permission,
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
from ..ui import (
    print_assistant_text,
    render_assistant_markdown,
    print_tool_call,
    print_tool_result,
    print_error,
    print_confirmation,
    print_cost,
    print_retry,
    print_info,
    print_sub_agent_start,
    print_sub_agent_end,
    start_spinner,
    stop_spinner,
)
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
from ..trace import make_tracer
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
from .compaction import (
    SNIPPABLE_TOOLS, SNIP_PLACEHOLDER, SNIP_THRESHOLD,
    MICROCOMPACT_IDLE_S, KEEP_RECENT_RESULTS, persist_large_result,
)
from .plan_mode import PlanModeMixin
from .anthropic_backend import AnthropicBackendMixin
from .openai_backend import OpenAIBackendMixin


# ─── Agent ───────────────────────────────────────────────────

# 前台子 agent 的回退 turn 上限：当 manifest 未声明 max-turns 时使用，
# 确保前台子 agent 永远有界（不至无限循环拖死父 loop）。
SUBAGENT_MAX_TURNS_FALLBACK = 50


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
    ):
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.use_openai = bool(api_base)
        self.is_sub_agent = is_sub_agent
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
        if not self.is_sub_agent:
            os.environ["NANOCODE_SESSION_ID"] = self.session_id
        if trace_parent is not None:
            self.tracer = trace_parent.child(self.session_id)
        else:
            self.tracer = make_tracer(self.session_id, enabled=trace_enabled)
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

        # Output buffer (sub-agents capture output)
        self._output_buffer: list[str] | None = None

        # Read-before-edit: track file read timestamps (absolutePath → mtime)
        self._read_file_state: dict[str, float] = {}

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

        # Separate message histories
        self._anthropic_messages: list[dict] = []
        self._openai_messages: list[dict] = []

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
                print(f"[mcp] Init failed: {e}", flush=True)

        self._aborted = False
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
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        self.tracer.emit(
            "session_end", input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens, turns=self.current_turns,
        )
        self.tracer.close()
        return {
            "text": text,
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out,
            },
        }

    # ─── Output helper ────────────────────────────────────────

    def _emit_text(self, text: str) -> None:
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)

    def _emit_block(self, text: str) -> None:
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            render_assistant_markdown(text)

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
        print_info("Conversation cleared.")

    def show_cost(self) -> None:
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        print_info(f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

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
        print_info(f"Session restored ({self._get_message_count()} messages).")

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
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        if self.use_openai:
            await self._compact_openai()
        else:
            await self._compact_anthropic()
        self.tracer.emit("compaction", kind="auto")
        print_info("Conversation compacted.")
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

    def _on_file_touched(self, inp: dict) -> None:
        """成功 read/write/edit 后触发：先嵌套发现 .nanocode/skills，再 paths 条件激活。"""
        fp = inp.get("file_path")
        if not fp:
            return
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
        if self.use_openai:
            self._budget_tool_results_openai()
            self._snip_stale_results_openai()
            self._microcompact_openai()
        else:
            self._budget_tool_results_anthropic()
            self._snip_stale_results_anthropic()
            self._microcompact_anthropic()

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

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
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
            return await tasks_tool.task_stop(self.task_manager, self._background_tasks, inp.get("task_id", ""))
        if name == "memory" and inp.get("action") == "recall" and inp.get("semantic"):
            return await self._recall_memory_semantic(inp.get("query", ""), int(inp.get("limit") or 5))
        if name == "memory" and inp.get("action") == "consolidate":
            return await self._spawn_memory_consolidate()
        if name in ("enter_plan_mode", "exit_plan_mode"):
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
            self._on_file_touched(inp)
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
                         background=False, max_turns=None, model=None) -> "Agent":
        """构造子 agent：集中权限继承。

        与 Claude Code / Kimi Code 对齐：
        - 子继承父 permission_mode（硬规则「子不得高于父」，不再无条件 bypass）。
        - 共享父 confirm_fn + _confirmed_paths（确认回流到父，同一引用）。
        - 共享 session_id + task_manager；trace 父子靠 trace_parent 对象。
        - is_sub_agent 工具表强制剔除 agent（子不能 spawn 孙）。
        - max_turns：前台子 agent 传入有界 turn 上限（_check_budget 强制），保证有界。
        - model：可选 per-agent 模型覆盖（manifest 'model'）；None 则继承父 model。

        background=True（detached 后台子 agent）：无 TTY，需确认的危险调用一律
        auto-deny（confirm_fn=_auto_deny_confirm 恒拒），并使用**新空集** confirmed_paths
        （不与父共享，后台确认不回流父），其余继承不变。
        """
        safe_tools = [t for t in tools if t.get("name") != "agent"]
        confirm_fn = _auto_deny_confirm if background else self.confirm_fn
        confirmed_paths = set() if background else self._confirmed_paths
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
            tools = (
                [t for t in self.tools if t["name"] in result["allowed_tools"]]
                if result.get("allowed_tools")
                else [t for t in self.tools if t["name"] != "agent"]
            )
            print_sub_agent_start("skill-fork", inp.get("skill_name", ""))
            sub_agent = self._build_sub_agent(
                system_prompt=result["prompt"],
                tools=tools,
                agent_type="coder",
                max_turns=self._bounded_sub_agent_max_turns(None),
            )
            try:
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.total_input_tokens += sub_result["tokens"]["input"]
                self.total_output_tokens += sub_result["tokens"]["output"]
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return f"Skill fork error: {e}"

        self._pending_skill_bodies.append((inp.get("skill_name", ""), result["prompt"]))
        return f'[skill "{inp.get("skill_name", "")}" loaded — its instructions follow in the next message]'

    def _current_provider(self) -> str:
        return "openai" if self.use_openai else "anthropic"

    def _persist_agent_messages(self, agent_id: str, sub_agent: "Agent") -> None:
        """Persist sub-agent messages to v2 session storage."""
        try:
            msgs = sub_agent._anthropic_messages if not sub_agent.use_openai else sub_agent._openai_messages
            _session_v2.write_agent_messages(self.session_id, agent_id, msgs)
        except Exception:
            pass

    # ─── Background sub-agent (detached, auto-deny-but-continue) ──

    def _summarize_subagent_text(self, text: str) -> str:
        """result_summary：截断子 agent 输出首 500 字符。"""
        return (text or "")[:500]

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
        print_sub_agent_start(agent_type, description)
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
            print_sub_agent_end(agent_type, description)
            raise
        except Exception as e:  # noqa: BLE001 — 构造/启动期异常也须落终态，detached 任务不能悬挂 running
            self.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(sub-agent error: {e})")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            print_sub_agent_end(agent_type, description)
            return

        if kind == "timeout":
            # SUBAGENT_STATUSES 现含 timed_out；保留既有约定：task=timed_out, sub=failed。
            self.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            print_sub_agent_end(agent_type, description)
            return
        if kind == "error":
            self.task_manager.update_task(
                task_id, status="failed", error=str(payload),
                result_summary=f"(sub-agent error: {payload})")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            print_sub_agent_end(agent_type, description)
            return

        result = payload  # kind == "ok"
        # 成功：token 累加进父 + result.md + result_summary + 持久化 messages
        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = self._write_subagent_result(task_id, text)
        self.task_manager.update_task(
            task_id, status="completed", result_path=result_path,
            result_summary=self._summarize_subagent_text(text))
        self.task_manager.update_subagent(agent_id, status="completed")
        self._persist_agent_messages(agent_id, sub_agent)
        print_sub_agent_end(agent_type, description)

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

        description = "memory consolidation"
        sub_rec = self.task_manager.create_subagent(
            type=self._MEMORY_CURATOR_TYPE, description=description,
            model=self.model, provider=self._current_provider(),
        )
        self.task_manager.update_subagent(sub_rec.id, status="running")
        task_rec = self.task_manager.create_task(
            "memory_consolidate", description, owner_agent_id=sub_rec.id)
        self.task_manager.update_subagent(sub_rec.id, task_id=task_rec.id)
        print_sub_agent_start(self._MEMORY_CURATOR_TYPE, description)
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
            print_sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            raise
        except asyncio.TimeoutError:
            self.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            print_sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            return
        except Exception as e:
            self.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(curator error: {e})")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            print_sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            return

        # curator 成功产出 JSON 提案：token 累加 + 持久化 + 写 result.md
        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = self._write_subagent_result(task_id, text)
        self.task_manager.update_subagent(agent_id, status="completed")
        self._persist_agent_messages(agent_id, sub_agent)

        # 确定性 parse+apply（宿主 Python，可回滚）。坏 JSON 不让 task failed，标 completed。
        try:
            plan = parse_consolidation_plan(text)
        except Exception:
            self.task_manager.update_task(
                task_id, status="completed", result_path=result_path,
                result_summary="Consolidation: no changes (unparseable plan)")
            print_sub_agent_end(self._MEMORY_CURATOR_TYPE, description)
            return

        apply_result = apply_plan(plan)
        self.task_manager.update_task(
            task_id, status="completed", result_path=result_path,
            result_summary=apply_result.summary_line())
        print_sub_agent_end(self._MEMORY_CURATOR_TYPE, description)

    # ─── Memory eval candidate generation (EVAL-mode curator) ──

    async def _spawn_memory_eval(self) -> str:
        """触发 eval 候选生成：EVAL-mode curator 子 agent 出候选 JSON →
        宿主逐条 add_pending（非法跳过）。无记忆短路。"""
        user_message = build_eval_curator_message()
        if user_message.startswith("No memory files"):
            return "No memories to generate eval candidates from."
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
        print_sub_agent_start(self._MEMORY_EVAL_CURATOR_TYPE, description)
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
            print_sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
            raise
        except asyncio.TimeoutError:
            self.task_manager.update_task(
                task_id, status="timed_out",
                result_summary=f"(timed out after {timeout_ms}ms)")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            print_sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
            return
        except Exception as e:
            self.task_manager.update_task(
                task_id, status="failed", error=str(e),
                result_summary=f"(eval curator error: {e})")
            self.task_manager.update_subagent(agent_id, status="failed")
            if sub_agent is not None:
                self._persist_agent_messages(agent_id, sub_agent)
            print_sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
            return

        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        text = result["text"] or ""
        result_path = self._write_subagent_result(task_id, text)
        self.task_manager.update_subagent(agent_id, status="completed")
        self._persist_agent_messages(agent_id, sub_agent)

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
            print_sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)
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
        print_sub_agent_end(self._MEMORY_EVAL_CURATOR_TYPE, description)

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
        # 工具入参 timeout_ms 优先；缺省时回退 manifest 'timeout-ms'（item 4）。
        tool_timeout_ms = inp.get("timeout_ms")

        # ─── run_in_background: detached subagent (auto-deny-but-continue) ───
        if inp.get("run_in_background"):
            if resume_id:
                return "Error: run_in_background cannot be combined with resume."
            bg_cfg = get_sub_agent_config(agent_type)
            bg_timeout = tool_timeout_ms if tool_timeout_ms is not None else bg_cfg.get("timeout_ms")
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
            eff_timeout = tool_timeout_ms if tool_timeout_ms is not None else config.get("timeout_ms")
            max_turns = self._bounded_sub_agent_max_turns(config.get("max_turns"))
            print_sub_agent_start(rec.type, description)
            sub_agent = self._build_sub_agent(
                system_prompt=config["system_prompt"],
                tools=config["tools"],
                agent_type=rec.type,
                max_turns=max_turns,
                model=rec.model or current_eff_model,
            )
            # Reload persisted messages
            history = _session_v2.read_agent_messages(self.session_id, resume_id)
            if not sub_agent.use_openai:
                sub_agent._anthropic_messages = history
            else:
                sub_agent._openai_messages = history

            # Update record status
            self.task_manager.update_subagent(resume_id, status="running")

            kind, payload = await self._run_foreground_subagent(
                sub_agent, prompt, eff_timeout, resume_id)
            if kind != "ok":
                # timeout：record 已在 helper 内标 'timed_out'；error：这里补标 failed。
                if kind == "error":
                    self.task_manager.update_subagent(resume_id, status="failed")
                self._persist_agent_messages(resume_id, sub_agent)
                print_sub_agent_end(rec.type, description)
                return payload  # type: ignore[return-value]
            result = payload  # type: ignore[assignment]
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            self.task_manager.update_subagent(resume_id, status="completed")
            self._persist_agent_messages(resume_id, sub_agent)
            print_sub_agent_end(rec.type, description)
            return result["text"] or "(Sub-agent produced no output)"

        # ─── fresh path ──────────────────────────────────────────
        config = get_sub_agent_config(agent_type)
        eff_timeout = tool_timeout_ms if tool_timeout_ms is not None else config.get("timeout_ms")
        max_turns = self._bounded_sub_agent_max_turns(config.get("max_turns"))
        eff_model = config.get("model") or self.model
        print_sub_agent_start(agent_type, description)
        # Register SubAgentRecord — 记录 EFFECTIVE 模型（manifest 覆盖优先），
        # 否则 resume 的 model-mismatch 校验会拿父模型自比、形同虚设。
        rec = self.task_manager.create_subagent(
            type=agent_type, description=description,
            model=eff_model, provider=self._current_provider(),
        )
        self.task_manager.update_subagent(rec.id, status="running")

        sub_agent = self._build_sub_agent(
            system_prompt=config["system_prompt"],
            tools=config["tools"],
            agent_type=agent_type,
            max_turns=max_turns,
            model=eff_model,
        )

        kind, payload = await self._run_foreground_subagent(
            sub_agent, prompt, eff_timeout, rec.id)
        if kind != "ok":
            if kind == "error":
                self.task_manager.update_subagent(rec.id, status="failed")
            self._persist_agent_messages(rec.id, sub_agent)
            print_sub_agent_end(agent_type, description)
            return payload  # type: ignore[return-value]
        result = payload  # type: ignore[assignment]
        self.total_input_tokens += result["tokens"]["input"]
        self.total_output_tokens += result["tokens"]["output"]
        self.task_manager.update_subagent(rec.id, status="completed")
        self._persist_agent_messages(rec.id, sub_agent)
        print_sub_agent_end(agent_type, description)
        return result["text"] or "(Sub-agent produced no output)"

    # ─── Shared ──────────────────────────────────────────────────

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
