"""CLI entry point and interactive REPL."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, FuzzyCompleter
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.patch_stdout import patch_stdout

from ..agent import Agent, AgentSession, AgentRuntime, RuntimeThread, ApprovalManager, AgentConfig
from ..tui import print_welcome, print_error, print_info, print_plan_for_approval, print_plan_approval_options
from ..session import get_latest_session_id
from ..session import v2 as _session_v2
from ..skills import discover_skills, resolve_skill_prompt, get_skill_by_name, execute_skill
from ..trajectory import (
    trajectory_enabled as _trajectory_enabled,
    trajectory_level as _trajectory_level,
)
from .trajectory_cmd import run as _run_trajectory_cmd
from ..tools.sandbox_shell import cleanup_persist_sandbox
from ..paths import history_file
from ..trust import is_trusted
from .commands.types import CommandContext, Local, Prompt, Control
from .commands.runner import dispatch, NOT_A_COMMAND
from .host import RuntimeHost
from .commands.builtin import build_registry

# 子命令分发表：未来加命令只需在此加一行 name -> handler(argv)->int
_SUBCOMMANDS = {"trajectory": _run_trajectory_cmd}


# REPL 输入哨兵：区分「用户输入空行」与「stdin EOF」。
EOF = object()      # stdin EOF (Ctrl-D)
CANCEL = object()   # line cancelled at prompt (Ctrl-C)


def _sigint_decision(is_processing: bool, sigint_count: int) -> tuple[str, int]:
    """SIGINT 决策（纯函数,可测）。turn 进行中 → ('abort', 0)（打断当前轮,不计退出）;
    空闲 → 计数,达 2 → ('exit', n);否则 ('warn', n)（提示再按一次退出）。"""
    if is_processing:
        return "abort", 0
    sigint_count += 1
    return ("exit" if sigint_count >= 2 else "warn"), sigint_count


# 内置斜杠命令的单一来源：从命令 registry 派生（取代旧的手维护列表，消除与 dispatch 的漂移）。
# exit/quit 是裸词（无 /），不进 /-gated 菜单，故不在 registry。
_REGISTRY = build_registry()
_BUILTIN_COMMANDS = [(s.name, s.description) for s in _REGISTRY.specs() if not s.is_hidden]


def _repl_commands_help() -> str:
    """REPL 命令帮助块（--help 与 /help 共用同一 registry 来源，CMD-P1）。"""
    lines = ["", "REPL commands:"]
    for s in _REGISTRY.specs():
        if s.is_hidden:
            continue
        left = f"  {s.name}" + (f" {s.arg_hint}" if s.arg_hint else "")
        lines.append(f"{left:<22} {s.description}")
    lines.append(f'{"  /<skill-name>":<22} Invoke a skill (e.g. /commit "fix types")')
    lines.append(f'{"  !<command>":<22} Run a shell command directly (your own command; bypasses agent + permissions)')
    return "\n".join(lines) + "\n"


class _CommandCompleter(Completer):
    """Yield slash-command candidates (built-ins + user-invocable skills), each
    with a right-aligned description (display_meta). Wrapped in FuzzyCompleter by
    the session so '/cmt' matches '/commit' (subsequence fuzzy match).

    Only fires when the buffer is a single token starting with '/'. Never raises
    (skill discovery failures degrade to built-ins only)."""
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name, desc in _BUILTIN_COMMANDS:
            yield Completion(name, start_position=-len(text),
                             display=name, display_meta=desc)
        try:
            for s in discover_skills():
                if s.user_invocable:
                    name = "/" + s.name
                    yield Completion(name, start_position=-len(text), display=name,
                                     display_meta=(s.description or "skill"))
        except Exception:
            pass



_session = None
_ephemeral_session = None


def _prime_history(session: PromptSession) -> None:
    """Force the FileHistory to load synchronously, up front.

    prompt_toolkit loads history lazily inside an asyncio *background task* the
    first time a prompt is drawn. Reading the file synchronously here (the same
    work History.load() caches into _loaded_strings on first use) means later
    reads come from memory, not disk. Best-effort: failure falls back to lazy
    load."""
    try:
        hist = session.history
        hist._loaded_strings = list(hist.load_history_strings())
        hist._loaded = True
    except Exception:
        pass


def _make_prime_buffer(session: PromptSession):
    """Build a `pre_run` hook that deterministically populates UP-arrow history.

    Why this is needed: every `prompt_async` call runs Buffer.reset(), which
    cancels/clears the history-load task and empties _working_lines. prompt_toolkit
    then repopulates _working_lines from history inside an asyncio *background
    task* on the first repaint. Pressing UP before that task runs recalls nothing
    — a race on every prompt (the previously typed line isn't recalled). `pre_run`
    fires synchronously once the app loop is live but before keys are read, so we
    copy the in-memory history into _working_lines ourselves and mark the load
    task done, eliminating the race. Best-effort + idempotent."""
    def _prime_buffer() -> None:
        try:
            buf = session.default_buffer
            if buf._load_history_task is not None:
                return  # already loaded/primed for this prompt
            # Iterate newest -> oldest so appendleft() yields working_lines
            # [oldest, ..., newest, current]; the first UP then recalls the most
            # recent entry (oldest-first iteration would reverse this and make UP
            # surface the oldest command first).
            for item in reversed(list(buf.history.get_strings())):
                buf._working_lines.appendleft(item)
                buf.working_index += 1
            fut = asyncio.get_event_loop().create_future()
            fut.set_result(None)
            buf._load_history_task = fut  # tell prompt_toolkit not to respawn the loader
        except Exception:
            pass  # fall back to prompt_toolkit's async load
    return _prime_buffer


def _get_session(*, input=None, output=None, persistent=True, bottom_toolbar=None) -> PromptSession:
    """Build the PromptSession (history + editing + completion).

    Production path (input/output both None) caches a single shared session bound
    to the real terminal. When an explicit input/output pair is supplied (tests
    injecting a pipe + DummyOutput), build a fresh, uncached session so the real
    prompt_toolkit code path is exercised without touching the global terminal.

    persistent=True uses the on-disk FileHistory (the main REPL prompt). Transient
    prompts (tool confirmations y/n, plan-approval 1-4) pass persistent=False so
    their answers never leak into ~/.nanocode/history and get recalled/autosuggested
    at a later command prompt."""
    def _history():
        return FileHistory(str(history_file())) if persistent else InMemoryHistory()

    # FuzzyCompleter wraps _CommandCompleter so '/cmt' subsequence-matches '/commit';
    # complete_while_typing pops the menu live as you type (Claude Code-style).
    # pattern keeps hyphens in the matched word (so '/task-s' narrows to /task-stop)
    # but excludes '/' so the inner completer still sees the leading slash and its
    # gate fires. The default word pattern breaks at '-' and would bury hyphenated
    # commands (e.g. /task-stop).
    def _completer():
        return FuzzyCompleter(_CommandCompleter(), pattern=r"^[a-zA-Z0-9_-]+")

    if input is not None or output is not None:
        session = PromptSession(
            history=_history(),
            auto_suggest=AutoSuggestFromHistory(),
            # enable_history_search MUST stay False: prompt_toolkit suppresses
            # complete_while_typing when history-search is on (mutually exclusive),
            # which would stop the '/' menu from popping up as you type. Plain
            # Up/Down history + Ctrl+R reverse-search still work without it.
            enable_history_search=False,
            completer=_completer(),
            complete_while_typing=True,
            bottom_toolbar=bottom_toolbar,
            input=input,
            output=output,
        )
        _prime_history(session)
        return session
    if not persistent:
        global _ephemeral_session
        if _ephemeral_session is None:
            _ephemeral_session = PromptSession(
                history=InMemoryHistory(),
                completer=None,
                complete_while_typing=False,
            )
        return _ephemeral_session
    global _session
    if _session is None:
        _session = PromptSession(
            history=_history(),
            auto_suggest=AutoSuggestFromHistory(),
            enable_history_search=False,  # keep False so complete_while_typing fires (see above)
            completer=_completer(),
            complete_while_typing=True,
            bottom_toolbar=bottom_toolbar,
        )
        _prime_history(_session)
    return _session


async def _async_read_line(prompt="", *, input=None, output=None, persistent=True,
                           default="", bottom_toolbar=None) -> object:
    """Read one line with full line editing + history via prompt_toolkit,
    cooperating with the asyncio loop (background tasks stay live).
    Ctrl-D -> EOF sentinel; Ctrl-C -> CANCEL sentinel.

    `input`/`output` are test seams (pipe + DummyOutput); production passes
    neither and the session binds to the real terminal. persistent=False routes
    a transient prompt (confirmation / plan menu) through an in-memory history so
    its answer is not written to the on-disk REPL history.

    SIGINT handling: prompt_async installs (and on some prompt_toolkit versions,
    e.g. 3.0.29, leaves at SIG_DFL on return) its own SIGINT handler. We snapshot
    the caller's handler and restore it in `finally`, so EVERY read site — the main
    loop AND the transient confirm/plan prompts — comes back with the REPL's
    handle_sigint intact. Without this, Ctrl-C during agent.chat after a
    confirmation would bypass agent.abort() and raise KeyboardInterrupt."""
    prev_sigint = signal.getsignal(signal.SIGINT)
    try:
        session = _get_session(input=input, output=output, persistent=persistent,
                               bottom_toolbar=bottom_toolbar)
        pre_run = _make_prime_buffer(session)
        if input is not None or output is not None:
            return await session.prompt_async(prompt, pre_run=pre_run, default=default)
        with patch_stdout():
            return await session.prompt_async(prompt, pre_run=pre_run, default=default)
    except EOFError:
        return EOF
    except KeyboardInterrupt:
        return CANCEL
    finally:
        try:
            signal.signal(signal.SIGINT, prev_sigint)
        except (TypeError, ValueError):
            pass  # not in main thread / no prior handler — leave as-is


async def _run_user_shell(command: str) -> str:
    """REPL 的 !<command>：用户直跑 shell，返回格式化输出。

    在线程池里跑同步 run_structured（不阻塞事件循环 / 不冻结后台任务）。
    不走权限系统——这是用户自己敲的命令，等同于在终端执行。
    """
    from ..tools import run_shell
    r = await asyncio.to_thread(run_shell.run_structured, {"command": command, "timeout": 120000})
    if r["timed_out"]:
        return f"$ {command}\n(timed out)"
    if r["error"] is not None:
        return f"$ {command}\nerror: {r['error']}"
    out = (r["stdout"] or "").rstrip()
    err = (r["stderr"] or "").rstrip()
    parts = [f"$ {command}"]
    if out:
        parts.append(out)
    if err:
        parts.append(err)
    if r["exit_code"] not in (0, None):
        parts.append(f"(exit {r['exit_code']})")
    return "\n".join(parts)


# Security: NO .env (repo-local OR user-level) may set nanocode's own security-sensitive
# env vars, the dynamic-linker injection vars, or interpreter-injection vars. Mirrors Codex's
# ILLEGAL_ENV_VAR_PREFIX ("CODEX_"). These belong to the operator's shell / CLI flags, not to
# .env content — otherwise a .env could disable the sandbox (NANOCODE_SHELL_SANDBOX=off),
# hijack the microVM launcher (NANOCODE_MSB_BIN / MSB_BIN), preload a shared object
# (LD_PRELOAD / DYLD_INSERT_LIBRARIES), hijack PATH, or inject code into the interpreter
# (PYTHONPATH / NODE_OPTIONS / BASH_ENV …). This is defense-in-depth that applies to ANY .env
# source, on top of the trust-gating that keeps an untrusted repo's .env from being read at all.
_DOTENV_BLOCKED_PREFIXES = (
    "NANOCODE_",   # 自有安全变量（sandbox 档 / msb 启动器…）—— 抄 Codex CODEX_ 前缀禁用
    "LD_",         # Linux 动态链接器：LD_PRELOAD / LD_LIBRARY_PATH / LD_AUDIT …
    "DYLD_",       # macOS 动态链接器：DYLD_INSERT_LIBRARIES / DYLD_LIBRARY_PATH …
)
_DOTENV_BLOCKED_NAMES = frozenset({
    "MSB_BIN", "PATH",
    "IFS", "ENV", "BASH_ENV", "SHELLOPTS",
    "PYTHONPATH", "NODE_OPTIONS", "PERL5LIB", "RUBYOPT",
})
# 前缀黑名单的显式例外：这些 NANOCODE_* 只是普通配置，不碰沙箱档位 / msb 启动器 /
# 数据目录 / 动态链接器 / 解释器注入，故允许 .env 设置。模型选择不引入新攻击面——
# base_url（OPENAI_/ANTHROPIC_BASE_URL）本就允许从 .env 来，模型名只是搭配它而已。
_DOTENV_ALLOWED_NAMES = frozenset({
    "NANOCODE_MODEL",   # 仅选择模型；无安全敏感性
})


def _is_blocked_dotenv_key(key: str) -> bool:
    up = key.upper()
    if up in _DOTENV_ALLOWED_NAMES:
        return False  # 显式放行的普通配置，优先于下面的前缀黑名单
    return up in _DOTENV_BLOCKED_NAMES or any(up.startswith(p) for p in _DOTENV_BLOCKED_PREFIXES)


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Minimal and zero-dependency. Supports `#` comments, blank lines, an optional
    leading `export `, and surrounding single/double quotes. Existing environment
    variables are NOT overwritten (an explicit `export` always wins). Silently does
    nothing if the file is absent. Security-sensitive / injection keys
    (`NANOCODE_*` / `LD_*` / `DYLD_*` / `MSB_BIN` / `PATH` / `PYTHONPATH` …) are never
    loaded from ANY `.env` (see `_is_blocked_dotenv_key`), with an explicit allowlist
    of benign exceptions (`NANOCODE_MODEL`, see `_DOTENV_ALLOWED_NAMES`).
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            if _is_blocked_dotenv_key(key):
                continue  # 任何 .env 都不得设 nanocode 安全敏感 / 注入类变量（见上）
            os.environ[key] = val


def _user_env_path() -> str:
    """用户级、可信的 .env 路径（抄 Codex `$CODEX_HOME/.env`）。

    落在 nanocode 的本地存储根（`paths.data_dir()`，默认 `~/.nanocode`，可被
    `NANOCODE_HOME` 覆盖）下的 `.env`。这是 operator 自己拥有的、与任何 repo 无关的
    可信来源，故总是加载。"""
    from ..paths import data_dir
    return str(data_dir() / ".env")


def _load_env_files() -> None:
    """加载 .env：用户级（总是，可信）+ repo 级（仅当 workspace 已被信任）。

    Codex 形状：不信任 / 首见的 repo 的 `./.env` 完全不读，根本不让其控制环境
    （PATH / LD_* / DYLD_* / NANOCODE_* / *_BASE_URL 等全部进不来）。新 / 未信任项目里
    API key 等仍可来自 shell env 或用户级 `~/.nanocode/.env`；repo `./.env` 在用户信任
    该目录后（下次运行）才生效。黑名单（`_is_blocked_dotenv_key`）对两个来源都生效。"""
    _load_dotenv(_user_env_path())          # 用户级：operator 自己的，总是读
    if is_trusted(Path.cwd()):              # 非交互、只读已记录的 trust（不弹对话）
        _load_dotenv()                      # repo ./.env：仅当该目录此前已被信任
    # 不信任 / 首见的 repo：其 ./.env 不读（一次性关掉 PATH/LD_*/NANOCODE_*/base_url 等整类）


# `handle_eval_command` / `_fmt_eval_row` 已迁入 commands/builtin（CMD-P0）；


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nanocode",
        description="nanocode — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--trajectory", action="store_true",
                        help="Project the canonical session tree into a trajectory (analysis/RL lane)")
    parser.add_argument("--trajectory-level", choices=["summary", "full"], default=None,
                        help="summary (default): drop heavy payloads, keep hash+summary; "
                             "full: keep full prompts/messages/tool results (may contain secrets)")
    parser.add_argument("--memory-backend", choices=["auto", "simplemem", "markdown", "off"],
                        default=None,
                        help="Long-term memory backend (default: auto)")
    parser.add_argument("--rpc", action="store_true",
                        help="Headless RPC mode: JSON-lines over stdio drive the same session (docs/17)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-turn token cost and MCP connection logs")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


async def run_repl(agent: Agent, lease=None) -> None:
    """Interactive REPL loop.

    docs/14 SessionLease：`lease` 是 main() 在 welcome 前激活的会话写者租约（已注入 agent._session_mgr）。
    REPL 退出（任何 break / 双 Ctrl-C）时释放当前 thread 的租约；rebind（/new /resume /clone）会把旧租约
    交接/关闭、新 thread 持新租约，故退出时只需释放 current_thread 的那把。"""

    # docs/14 P1：会话宿主——把"当前 thread"从固定局部闭包解放出来。lifecycle 替换（/new /resume
    # /clone /fork、子父导航）由 runtime 原子换掉整组 Agent/AgentSession/RuntimeThread，命令 handler
    # 永远对 host.current_thread 操作、不缓存 agent/session。这里显式构造 session+thread（保留
    # AgentSession 这个可注入 seam）并注册进 registry——而非 adopt（adopt 在 runtime 内部建 session，
    # 会绕过测试对 cli.AgentSession 的替身）。lease 随 thread 持有，退出时 release。
    _runtime = AgentRuntime()
    _thread = _runtime.register(RuntimeThread(_runtime, agent, AgentSession(agent), lease=lease))
    # docs/17 Phase 1：TUI 是挂在 runtime/session 上的订阅客户端——渲染从事件流派生，不再由 core
    # 经 EventSink 投影。host 持有它并在 thread 替换时重新订阅。
    from .terminal_client import TerminalClient
    _client = TerminalClient()
    _host = RuntimeHost(_runtime, _thread, registry=_REGISTRY, interactive=sys.stdout.isatty(),
                        client=_client)

    # CMD-P2.5 / docs/15 Phase 7：普通 chat / skill turn **一律**经 RuntimeThread.run 驱动
    # （取 host 的 current_thread）。逃生阀 NANOCODE_REPL_VIA_RUNTIME 已删——runtime 是唯一 turn 路径。

    async def _drive_turn(prompt: str) -> None:
        await _host.current_thread.run(prompt)

    # pi /fork 语义：选中的历史 prompt 放回编辑器（下一次主读取的 default 文本，one-shot）。
    _pending_prefill = {"text": ""}

    async def _apply_control(host: RuntimeHost, ctrl: Control) -> None:
        """消费 lifecycle Control（docs/14 P2）：先过 host.can_switch() fail-closed 闸，再交
        runtime-owned replacement（thread_new / thread_resume → rebind_session）。handler 只发信号，
        所有 live agent 替换都在此统一路由。"""
        ok, reason = host.can_switch()
        if not ok:
            print_info(f"cannot switch sessions right now: {reason}")
            return
        action, payload = ctrl.action, (ctrl.payload or {})
        if action == "replace_thread" and payload.get("kind") == "new":
            host.runtime.thread_new(host)          # rebind 已 sink.info "Session → ..."
        elif action == "replace_thread" and payload.get("kind") == "clone":
            # pi /clone：复制当前 active branch 到当前 leaf → 新 session；编辑器为空。
            if host.runtime.thread_clone(host, payload.get("sourceSid")) is None:
                print_error("clone failed (no canonical tree / nothing to clone).")
        elif action == "replace_thread" and payload.get("kind") == "fork":
            # pi /fork：复制到选中 user 消息**之前** → 新 session；该 prompt 放回编辑器。
            if host.runtime.thread_fork(host, payload.get("sourceSid"),
                                        payload.get("userEntryId")) is None:
                print_error("fork failed (no canonical tree / unknown entry).")
            else:
                _pending_prefill["text"] = payload.get("prefill") or ""
        elif action == "resume":
            sid = payload.get("sessionId")
            if sid == host.current_thread.thread_id:
                print_info(f"Already on session {sid}.")
                return
            if payload.get("fork"):
                # --fork：把目标 clone 成新 session 切入（绝不做第二个 writer，docs/14 §5.2）
                if host.runtime.thread_clone(host, sid) is None:
                    print_error(f"cannot fork-resume '{sid}'.")
                return
            from ..session.tree import SessionBusyError
            try:
                if host.runtime.thread_resume(host, sid) is None:
                    print_error(f"cannot resume '{sid}' (no canonical tree).")
            except SessionBusyError:
                print_error(f"session '{sid}' is busy (another writer holds it). "
                            f"Use `/resume {sid} --fork` to fork it into a new session.")
        else:
            print_info(f"(control '{action}' is not wired yet — coming in a later phase)")

    async def confirm_fn(message: str) -> bool:
        answer = await _async_read_line("  Allow? (y/n): ", persistent=False)
        if answer is EOF or answer is CANCEL:
            return False
        return answer.lower().startswith("y")

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            choice = await _async_read_line("  Enter choice (1-4): ", persistent=False)
            if choice is EOF or choice is CANCEL:
                return {"choice": "manual-execute"}
            choice = choice.strip()
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                feedback = await _async_read_line("  Feedback (what to change): ", persistent=False)
                feedback = "" if feedback in (EOF, CANCEL) else feedback.strip()
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print("  Invalid choice. Enter 1, 2, 3, or 4.")

    # CMD-P2.5：审批经 ApprovalManager 注入（取代散点 agent.set_confirm_fn / set_plan_approval_fn）。
    ApprovalManager(confirm_fn=confirm_fn, plan_approval_fn=plan_approval_fn).attach(agent)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        # turn 进行中（thinking/streaming/工具）：Ctrl-C 打断当前 turn——优雅 abort（置 _aborted +
        # cancel current task），run_turn 把 CancelledError 吞成 _aborted 正常返回,控制权回 prompt,
        # 输入框随下一轮 _async_read_line 重绘（不退出、不消失）。空闲时保留「按两次退出」。
        a = _host.current_thread.agent
        action, sigint_count = _sigint_decision(a.is_processing, sigint_count)
        if action == "abort":
            a.abort()
            print("\n  Interrupted — back to prompt.")
            return
        if action == "exit":
            print("\nBye!\n")
            try:
                _host.current_thread.release_lease()   # 释放当前 thread 的会话写锁后退出
            except Exception:
                pass
            sys.exit(0)
        print("\n  Press Ctrl+C again to exit.")

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    def _footer_toolbar():
        """bottom_toolbar 回调:从当前 thread 的 status() 组装 Pi 两行页脚（ANSI）。
        高频重绘——RuntimeThread.status() 是稳定只读快照（docs/17 Phase 5a，不再 reach 进 Agent
        私有面）+ git 分支 TTL 缓存,失败返回空串（绝不让页脚拖垮 REPL）。"""
        try:
            from .interactive.footer import FooterState, git_branch, render_footer
            from prompt_toolkit.application import get_app
            st = _host.current_thread.status()
            cwd = st["cwd"]
            state = FooterState(
                cwd=cwd,
                home=os.path.expanduser("~"),
                branch=git_branch(cwd),
                session_name=st["session_name"],
                input_tokens=st["input_tokens"],
                output_tokens=st["output_tokens"],
                cost_usd=st["cost_usd"],
                context_used=st["input_tokens"],
                context_window=st["context_window"],
                model=st["model"],
                thinking=st["thinking"],
            )
            try:
                width = get_app().output.get_size().columns
            except Exception:
                width = None
            return ANSI("\n".join(render_footer(state, width)))
        except Exception:
            return ""

    cancel_count = 0
    while True:
        _prefill, _pending_prefill["text"] = _pending_prefill["text"], ""
        line = await _async_read_line(ANSI("\n\x1b[1;32m> \x1b[0m"), default=_prefill,
                                      bottom_toolbar=_footer_toolbar)
        # SIGINT handler is restored inside _async_read_line (covers this read and
        # the transient confirm/plan prompts too), so Ctrl-C during agent.chat
        # always reaches handle_sigint.
        if line is EOF:
            print("\nBye!\n")
            break
        if line is CANCEL:
            # At the prompt Ctrl-C is a raw-mode keypress (not a SIGINT), so it
            # comes back as CANCEL rather than firing handle_sigint. First press
            # clears the current line; a second consecutive press exits — matching
            # the old two-Ctrl-C-to-quit behavior. Ctrl-D / `exit` also quit.
            cancel_count += 1
            if cancel_count >= 2:
                print("\nBye!\n")
                break
            print("  (Press Ctrl-C again, or Ctrl-D, to exit)")
            continue
        cancel_count = 0
        inp = line.strip()
        # 中文输入法常打出全角斜杠／：命令以 / 开头，开头的 ／ 归一为半角，
        # 否则 "／memory" 不匹配任何命令分支、被当成普通文本发给 AI。
        if inp[:1] == "／":
            inp = "/" + inp[1:]
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        # !<command>：用户直跑 shell（等同自己在终端执行），不进 AI 对话、不走
        # 模型权限系统——权限系统约束的是模型行为，用户自己敲的命令直接执行。
        if inp.startswith("!"):
            cmd = inp[1:].strip()
            if cmd:
                print_info(await _run_user_shell(cmd))
            continue

        # REPL slash commands —— 经 registry 分发（most-specific-first；非命令回退 skill/chat）。
        # !shell / exit / quit / 全角归一 / 未知斜杠 fallthrough 仍在本 loop 处理（见上下）。
        _result = await dispatch(inp, _REGISTRY, _host.context())
        if _result is not NOT_A_COMMAND:
            # 按 CommandResult variant 路由（Codex cross-review MED）：Prompt 驱动一个 turn；
            # Local 打印 output / 可退出；Control 经 _apply_control 路由 lifecycle（P2 起接通）。
            if isinstance(_result, Prompt):
                await _drive_turn(_result.prompt)
                continue
            if isinstance(_result, Local):
                if _result.output:
                    print_info(_result.output)
                if _result.exit_repl:
                    print("\nBye!\n")
                    break
                continue
            if isinstance(_result, Control):
                await _apply_control(_host, _result)
                continue
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.hooks:
                        agent._register_skill_hooks(skill)
                    if skill.context == "fork":
                        result = execute_skill(skill.name, cmd_args)
                        if result:
                            await _drive_turn(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        resolved = resolve_skill_prompt(skill, cmd_args)
                        await _drive_turn(resolved)
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await _drive_turn(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))

    # docs/14 SessionLease：REPL 退出（正常 break / exit / quit / 双 Ctrl-C-at-prompt）→ 释放当前
    # thread 的会话写锁。rebind 已交接/关闭旧租约，故只需释放 current_thread 的那把（幂等）。
    try:
        _host.current_thread.release_lease()
    except Exception:
        pass


def main() -> None:
    _argv = sys.argv[1:]
    if _argv and _argv[0] in _SUBCOMMANDS:
        raise SystemExit(_SUBCOMMANDS[_argv[0]](_argv[1:]))
    _load_env_files()
    args = parse_args()

    if args.help:
        print("""
Usage: nanocode [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: claude-opus-4-6, or NANOCODE_MODEL env)
  --api-base URL      Use OpenAI-compatible API endpoint (key via env var)
  --resume            Resume the last session
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --trajectory        Project the canonical session tree into a trajectory (analysis/RL lane)
  --trajectory-level {summary,full}
                      summary (default): drop heavy payloads, keep hash + summary.
                      full: keep full prompts/messages/tool results — may contain secrets.
  --verbose           Print per-turn token cost and MCP connection logs (default: quiet)
  --memory-backend B  Long-term memory: auto|simplemem|markdown|off (default: auto)
  --help, -h          Show this help

Environment:
  API keys are read from env vars: ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL)
  or OPENAI_API_KEY (+ OPENAI_BASE_URL). A .env file in the current directory is
  loaded automatically (existing environment variables take precedence).
""" + _repl_commands_help() + """
Examples:
  nanocode "fix the bug in src/app.py"
  nanocode --yolo "run all tests and fix failures"
  nanocode --plan "how would you refactor this?"
  nanocode --max-cost 0.50 --max-turns 20 "implement feature X"
  OPENAI_API_KEY=sk-xxx nanocode --api-base https://aihubmix.com/v1 --model gpt-4o "hello"
  nanocode --resume
  nanocode  # starts interactive REPL

  nanocode trajectory             # list sessions available as trajectories
  nanocode trajectory show <id>   # per-step table + metrics summary for a session
  nanocode trajectory export <id> # export a session as a trajectory bundle (--out DIR)
""")
        sys.exit(0)

    from ..tui import set_verbose
    set_verbose(args.verbose or os.environ.get("NANOCODE_VERBOSE", "").lower() in ("1", "true", "yes"))
    permission_mode = _resolve_permission_mode(args)
    model = args.model or os.environ.get("NANOCODE_MODEL", "claude-opus-4-6")
    api_base = args.api_base

    # Resolve API config
    resolved_api_base = api_base
    resolved_api_key: str | None = None
    resolved_use_openai = bool(api_base)

    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
        resolved_api_key = os.environ["OPENAI_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("OPENAI_BASE_URL")
        resolved_use_openai = True
    elif os.environ.get("ANTHROPIC_API_KEY"):
        resolved_api_key = os.environ["ANTHROPIC_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("ANTHROPIC_BASE_URL")
        resolved_use_openai = False
    elif os.environ.get("OPENAI_API_KEY"):
        resolved_api_key = os.environ["OPENAI_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("OPENAI_BASE_URL")
        resolved_use_openai = True

    if not resolved_api_key and api_base:
        resolved_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        resolved_use_openai = True

    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL) for Anthropic format,\n"
            "  or OPENAI_API_KEY + OPENAI_BASE_URL for OpenAI-compatible format."
        )
        sys.exit(1)

    # 工作区信任闸：必须在构造 Agent（及其触发的项目侧配置加载）之前。
    # 交互且不信任 → 弹 y/n（y 记住并继续，否则退出）；非交互 → 隐式信任。
    from ..trust import ensure_workspace_trust
    _interactive = not bool(args.prompt) and sys.stdin.isatty()
    workspace_trusted = ensure_workspace_trust(Path.cwd(), interactive=_interactive)

    # Resolve resume BEFORE constructing Agent (so we adopt the session_id).
    # docs/14 SessionLease：--resume = 恢复最近的 canonical session（latest header）；无则保持新建。
    # canonical 树是唯一 resume 权威（docs/16 C-3：legacy flat/v2 发现面已删，latest 必有树）。
    adopt_sid = None
    if args.resume:
        adopt_sid = get_latest_session_id()
        if adopt_sid is None:
            print_info("No previous sessions found.")

    # Long-term memory backend (CLI > env > auto). auto silently degrades to
    # markdown when no embeddings endpoint is configured; explicit --memory-backend
    # simplemem warns on degradation.
    from ..memory import select_backend
    mem_backend = select_backend(args.memory_backend, on_warning=print_info)

    # trajectory 采集（canonical 树的 DERIVED 投影 / RL 分析专用）：
    # 显式 flag 或 NANOCODE_TRAJECTORY 环境变量开启；level 非法/缺省退回 "summary"。
    traj_on = _trajectory_enabled(args.trajectory)
    traj_lvl = _trajectory_level(args.trajectory_level)

    # docs/14 P2：Agent 构造收敛到 AgentConfig（CLI/SDK/AppServer 共用 bootstrap 数据载体）。
    # main() 仍负责交互 I/O（trust gate / memory backend 选择 / resume 解析），把结果灌进 config。
    agent = AgentConfig(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
        trajectory_enabled=traj_on,
        trajectory_level=traj_lvl,
        workspace_trusted=workspace_trusted,
        session_id=adopt_sid,
        memory_backend=mem_backend,
    ).build_agent()

    if traj_on:
        if traj_lvl == "full":
            print_info(f"trajectory: on (level=full) — records full prompts/messages/tool "
                       f"results — may contain secrets")
        else:
            print_info(f"trajectory: on (level={traj_lvl})")
    elif args.trajectory_level is not None:
        # --trajectory-level 不开启采集（需 --trajectory / NANOCODE_TRAJECTORY）——给出可见提示，
        # 否则用户以为开了 full 采集却毫无效果（审阅 LOW UX）。
        print_info("note: --trajectory-level has no effect without --trajectory "
                   "(or NANOCODE_TRAJECTORY=1)")

    # docs/14 SessionLease：在 welcome / 任何 turn 之前激活会话写者租约（runtime 拥有 active writer）。
    # new：open_or_create 建空树 + 持锁；--resume：open 已有树 + 持锁。busy/corrupt → fail-closed 退出。
    from ..session.lease import SessionLease
    from ..session.tree import SessionBusyError, SessionTreeError
    try:
        _lease = SessionLease.open_or_create(agent.session_id)
    except SessionBusyError:
        print_error(f"session '{agent.session_id}' is busy — another nanocode process holds its "
                    f"writer lock. Start a new session, or resume a different one.")
        sys.exit(1)
    try:
        _built = _lease.manager.build_context()      # 校验树可折叠（corrupt → 退出，不静默）
    except SessionTreeError as e:
        _lease.close()
        print_error(f"corrupt session tree for '{agent.session_id}': {e}")
        sys.exit(1)
    agent._session_mgr = _lease.manager              # 请求按轮从树重渲染（docs/16 #3c），无需预装载
    if adopt_sid is not None:
        agent._reload_task_state(_session_v2.read_state(adopt_sid)
                                 if _session_v2.is_v2_session(adopt_sid) else None)
        print_info(f"Session resumed: {adopt_sid} ({len(_built.messages)} messages).")

    prompt = " ".join(args.prompt) if args.prompt else None

    def _finish_session() -> None:
        try:
            cleanup_persist_sandbox(agent.session_id)
        except Exception:
            pass

    if args.rpc:
        # Headless RPC mode（docs/17 Phase 5b）：JSON-lines over stdio 驱动同一 session,无 TUI。
        from .rpc import run_rpc_mode
        try:
            asyncio.run(run_rpc_mode(agent, _lease))
        finally:
            try:
                _lease.close()
            except Exception:
                pass
            _finish_session()
    elif prompt:
        # One-shot mode —— docs/15 Phase 7：headless 路径同样**仅**经 RuntimeThread.run,不绕过 runtime
        # （逃生阀已删）。

        async def _one_shot() -> None:
            from .terminal_client import TerminalClient
            th = AgentRuntime().adopt(agent, lease=_lease)
            th.subscribe(TerminalClient().on_event)   # docs/17 Phase 1：headless 也经订阅客户端渲染
            await th.run(prompt)

        try:
            asyncio.run(_one_shot())
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
        finally:
            try:
                _lease.close()                 # 释放会话写锁（一次性模式结束）
            except Exception:
                pass
            _finish_session()
    else:
        # Interactive REPL
        try:
            asyncio.run(run_repl(agent, _lease))
        finally:
            _finish_session()                  # run_repl 退出时已 release 当前 thread 的 lease


if __name__ == "__main__":
    main()
