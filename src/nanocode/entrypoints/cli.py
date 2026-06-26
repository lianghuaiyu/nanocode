"""CLI entry point and interactive REPL."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ..runtime import AgentRuntime, RuntimeThread, AgentConfig
from ..session import get_latest_session_id
from ..trajectory import (
    trajectory_enabled as _trajectory_enabled,
    trajectory_level as _trajectory_level,
)
from ..paths import history_file
from ..trust import is_trusted
from .commands.types import Local, Prompt, Control
from .commands.runner import dispatch, NOT_A_COMMAND
from .host import RuntimeHost
from .commands.builtin import build_registry

def _run_trajectory_cmd(argv) -> int:
    from .trajectory_cmd import run
    return run(argv)


# 子命令分发表：未来加命令只需在此加一行 name -> handler(argv)->int
_SUBCOMMANDS = {"trajectory": _run_trajectory_cmd}


def print_welcome(*args, **kwargs) -> None:
    from ..tui import print_welcome as _print_welcome
    _print_welcome(*args, **kwargs)


def print_error(*args, **kwargs) -> None:
    from ..tui import print_error as _print_error
    _print_error(*args, **kwargs)


def print_info(*args, **kwargs) -> None:
    from ..tui import print_info as _print_info
    _print_info(*args, **kwargs)


# 内置斜杠命令的单一来源：从命令 registry 派生（取代旧的手维护列表，消除与 dispatch 的漂移）。
# exit/quit 是裸词（无 /），不进 /-gated 菜单，故不在 registry。
_REGISTRY = build_registry()


def _repl_commands_help() -> str:
    """REPL 命令帮助块（--help 与 /help 共用同一 registry 来源，CMD-P1）。"""
    lines = ["", "REPL commands:"]
    for s in _REGISTRY.specs():
        if s.is_hidden:
            continue
        left = f"  {s.name}" + (f" {s.arg_hint}" if s.arg_hint else "")
        lines.append(f"{left:<22} {s.description}")
    lines.append(f'{"  /<skill-name>":<22} Invoke a skill (e.g. /commit "fix types")')
    lines.append(f'{"  !<command>":<22} Run a shell command via runtime audit (not a model tool call)')
    return "\n".join(lines) + "\n"


# Security: NO .env (repo-local OR user-level) may set nanocode's own security-sensitive
# env vars, the dynamic-linker injection vars, or interpreter-injection vars. Mirrors Codex's
# ILLEGAL_ENV_VAR_PREFIX ("CODEX_"). These belong to the operator's shell / CLI flags, not to
# .env content — otherwise a .env could weaken the sandbox profile,
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
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                        help="Continue the most recent session")
    parser.add_argument("-r", "--resume-picker", action="store_true",
                        help="Browse and select from past sessions at startup")
    parser.add_argument("--session", metavar="ID_OR_PATH", default=None,
                        help="Use a specific session ID, partial ID, or managed session.jsonl path")
    parser.add_argument("--fork", dest="fork_session", metavar="ID_OR_PATH", default=None,
                        help="Fork a session ID, partial ID, or managed session.jsonl path into a new session")
    parser.add_argument("--name", "-n", dest="session_name", default=None,
                        help="Set the session display name at startup")
    parser.add_argument("--no-session", action="store_true",
                        help="Ephemeral mode; delete this run's generated session on exit")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--map-tokens", type=int, default=None,
                        help="Suggested tokens for repo map; use 0 to disable")
    parser.add_argument("--map-refresh", choices=["auto", "always", "files", "manual"], default=None,
                        help="Control how often the repo map is refreshed")
    parser.add_argument("--map-multiplier-no-files", type=float, default=None,
                        help="Repo map token multiplier when no files have been read/modified")
    parser.add_argument("--trajectory", action="store_true",
                        help="Project the canonical session tree into a trajectory (analysis/RL lane)")
    parser.add_argument("--trajectory-level", choices=["summary", "full"], default=None,
                        help="summary (default): drop heavy payloads, keep hash+summary; "
                             "full: keep full prompts/messages/tool results (may contain secrets)")
    parser.add_argument("--memory-backend", choices=["simplemem", "markdown", "off"],
                        default=None,
                        help="Long-term memory backend (default: markdown)")
    parser.add_argument("--rpc", action="store_true",
                        help="Headless RPC mode: JSON-lines over stdio drive the same session (docs/17)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-turn token cost and MCP connection logs")
    parser.add_argument("--sandbox-profile",
                        choices=["default", "read-only", "strict", "vm", "danger-full-access"],
                        default=None,
                        help="Sandbox profile for run_shell (default: native-first workspace-write)")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    argv = sys.argv[1:]
    option_argv = argv[:argv.index("--")] if "--" in argv else argv
    for arg in option_argv:
        if arg == "--resume" or arg.startswith("--resume=") or arg == "--resume-last":
            parser.error("--resume has been removed; use -c/--continue or --session <id>")
    return parser.parse_args(argv)


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


def _resolve_session_arg(value: str) -> tuple[str | None, str | None]:
    """Resolve a startup session target from ID, unique prefix, or managed session path."""
    import json
    from ..session.manager import SessionManager, _scan_headers, session_file

    raw = (value or "").strip()
    if not raw:
        return None, "empty session target"

    p = Path(raw).expanduser()
    if p.exists():
        path = p / "session.jsonl" if p.is_dir() else p
        try:
            header = json.loads(path.open(encoding="utf-8").readline() or "{}")
        except Exception as e:
            return None, f"could not read session path {path}: {e}"
        if header.get("type") != "session_start":
            return None, f"unsupported session file format at {path}"
        sid = header.get("sessionId") or (header.get("data") or {}).get("sessionId")
        if not sid:
            return None, f"session path {path} does not contain a sessionId"
        try:
            if path.resolve() != session_file(sid).resolve():
                return None, f"session path {path} is outside nanocode's managed session store"
        except Exception:
            return None, f"session path {path} is outside nanocode's managed session store"
        return sid, None

    known = {sid for sid, _ps in _scan_headers()}
    matches = ([sid for sid in known if sid == raw] or [sid for sid in known if sid.startswith(raw)])
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"ambiguous session '{raw}' ({len(matches)} matches); use a longer prefix"
    if SessionManager.exists(raw):
        return raw, None
    return None, f"unknown session '{raw}'"


async def _select_startup_session(cwd: str) -> str | None:
    """Run the same session picker used by `/resume` before the main thread starts."""
    from ..tui.rich_app import RichApp
    from ..tui.session_pages.resume import run_sessions

    app = RichApp(registry=_REGISTRY, completer=_make_file_completer())
    selected: dict[str, str | None] = {"sid": None}

    async def _drive_picker() -> None:
        try:
            res = await run_sessions(current_sid=None, cwd=cwd, host=app)
            if res and res.get("action") == "resume":
                selected["sid"] = res.get("sid")
        finally:
            app.request_exit()

    task = asyncio.create_task(_drive_picker())
    await app.run()
    await task
    return selected["sid"]


def _cleanup_ephemeral_session(session_id: str) -> None:
    """Best-effort removal for `--no-session` sessions created by this process."""
    import shutil
    from ..session.manager import children, session_root

    stack = [session_id]
    seen: set[str] = set()
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        try:
            stack.extend(children(sid))
        except Exception:
            pass
        try:
            shutil.rmtree(session_root(sid), ignore_errors=True)
        except Exception:
            pass


def _make_file_completer():
    """注入给 TUI 的文件搜索闭包——保持 TUI 嵌入式边界(TUI 不直接碰 fs/agent)。

    `complete(token, mode)`:mode='mention' 仓库文件子串搜(@-提及);mode='path' 目录前缀补全(Tab)。
    返回相对路径列表(目录带末尾 '/')。git ls-files 优先(尊重 .gitignore),否则 os.walk 兜底。
    """
    import os
    import subprocess

    cwd = os.getcwd()
    _cache: dict = {}

    def _repo_files(limit: int = 4000) -> list[str]:
        if "files" in _cache:
            return _cache["files"]
        files: list[str] = []
        try:
            r = subprocess.run(["git", "ls-files"], cwd=cwd, capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                files = [f for f in r.stdout.split("\n") if f]
        except Exception:
            files = []
        if not files:
            _SKIP = {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache"}
            for root, dirs, names in os.walk(cwd):
                dirs[:] = [d for d in dirs if d not in _SKIP]
                for n in names:
                    files.append(os.path.relpath(os.path.join(root, n), cwd))
                    if len(files) >= limit:
                        break
                if len(files) >= limit:
                    break
        _cache["files"] = files
        return files

    def complete(token: str, mode: str = "mention") -> list[str]:
        token = token or ""
        if mode == "path":
            d, base = os.path.dirname(token), os.path.basename(token)
            scan = os.path.join(cwd, d) if d else cwd
            try:
                out = []
                for e in os.scandir(scan):
                    if not e.name.startswith(base):
                        continue
                    if e.name.startswith(".") and not base.startswith("."):
                        continue                      # 隐藏 dotfile,除非用户已键入前导 '.'
                    rel = os.path.join(d, e.name) if d else e.name
                    out.append(rel + ("/" if e.is_dir() else ""))
                out.sort(key=lambda x: (not x.endswith("/"), x.lower()))
                return out[:50]
            except Exception:
                return []
        q = token.lower()
        files = _repo_files()
        if not q:
            return files[:50]
        return [f for f in files if q in f.lower()][:50]

    return complete


async def run_repl(thread: RuntimeThread, *, input=None, output=None) -> None:
    """Interactive REPL loop.

    REPL 退出（任何 break / 双 Ctrl-C）时释放当前 thread 的租约；rebind（/new /resume /clone）
    会把旧租约交接/关闭、新 thread 持新租约，故退出时只需释放 current_thread 的那把。"""
    if not isinstance(thread, RuntimeThread):
        raise TypeError("run_repl() requires a RuntimeThread")
    _thread = thread
    _runtime = _thread._runtime
    # docs/18 Rich Live：交互客户端是挂在 runtime/session 上的订阅端（drop-in 换掉旧 prompt_toolkit
    # TuiApp）。RichApp 采用 Codex 式 inline viewport：流式内容和刚完成的一轮先留在 Live active
    # cell，下一轮开始时再写入 Live 上方的终端 scrollback，避免完成瞬间输入框跳位。
    # RichApp 自管历史（paths.history_file）；命令补全 v2 再接。input/output 是测试 seam（fd / Console）。
    from ..tui.rich_app import RichApp
    _app = RichApp(input=input, output=output, registry=_REGISTRY, completer=_make_file_completer())
    _host = RuntimeHost(_runtime, _thread, registry=_REGISTRY, interactive=sys.stdout.isatty(),
                        client=_app)

    # CMD-P2.5 / docs/15 Phase 7：普通 chat / skill turn **一律**经 RuntimeThread.run 驱动
    # （取 host 的 current_thread）。逃生阀 NANOCODE_REPL_VIA_RUNTIME 已删——runtime 是唯一 turn 路径。

    # docs/18 fix：app 运行中的人面输出必须经 run_in_terminal 印到 app 之上（直接 print 会被连续重绘
    # 覆盖）。_say/_err 经 TuiApp.print_above 走该路径；命令/控制/skill 的提示都用它。
    def _say(text) -> None:
        _app.print_above(str(text))

    def _err(text) -> None:
        _app.print_above(str(text), error=True)

    def _refresh_transcript() -> None:
        refresh = getattr(_app, "refresh_transcript", None)
        if callable(refresh):
            refresh()

    async def _drive_turn(prompt: str) -> None:
        await _host.current_thread.run(prompt)

    async def _apply_control(host: RuntimeHost, ctrl: Control) -> None:
        """消费 lifecycle Control（docs/14 P2）：先过 host.can_switch() fail-closed 闸，再交
        runtime-owned replacement（thread_new / thread_resume → rebind_session）。handler 只发信号，
        所有 live agent 替换都在此统一路由。"""
        ok, reason = host.can_switch()
        if not ok:
            _say(f"cannot switch sessions right now: {reason}")
            return
        action, payload = ctrl.action, (ctrl.payload or {})
        if action == "replace_thread" and payload.get("kind") == "new":
            host.runtime.thread_new(host)          # rebind 已 sink.info "Session → ..."
        elif action == "replace_thread" and payload.get("kind") == "clone":
            # pi /clone：复制当前 active branch 到当前 leaf → 新 session；编辑器为空。
            if host.runtime.thread_clone(host, payload.get("sourceSid")) is None:
                _err("clone failed (no canonical tree / nothing to clone).")
            else:
                _refresh_transcript()
        elif action == "replace_thread" and payload.get("kind") == "fork":
            # pi /fork：复制到选中 user 消息**之前** → 新 session；该 prompt 放回编辑器（app 输入框）。
            if host.runtime.thread_fork(host, payload.get("sourceSid"),
                                        payload.get("userEntryId")) is None:
                _err("fork failed (no canonical tree / unknown entry).")
            else:
                _refresh_transcript()
                _app.input_buffer.text = payload.get("prefill") or ""
        elif action == "resume":
            sid = payload.get("sessionId")
            if sid == host.current_thread.thread_id:
                _refresh_transcript()
                return
            if payload.get("fork"):
                # --fork：把目标 clone 成新 session 切入（绝不做第二个 writer，docs/14 §5.2）
                if host.runtime.thread_clone(host, sid) is None:
                    _err(f"cannot fork-resume '{sid}'.")
                else:
                    _refresh_transcript()
                return
            from ..session.tree import SessionBusyError
            try:
                if host.runtime.thread_resume(host, sid) is None:
                    _err(f"cannot resume '{sid}' (no canonical tree).")
                else:
                    _refresh_transcript()
            except SessionBusyError:
                _err(f"session '{sid}' is busy (another writer holds it). "
                     f"Use `/resume {sid} --fork` to fork it into a new session.")
        else:
            _say(f"(control '{action}' is not wired yet — coming in a later phase)")

    # docs/18：审批/plan 不再读一行，改成 TuiApp 的 modal future（confirm_fn → choice modal；
    # plan_approval_fn → 1-4 modal）。
    _thread.attach_approvals(confirm_fn=_app.confirm_fn, plan_approval_fn=_app.plan_approval_fn)

    async def _submit(text: str) -> None:
        """用户提交一行：命令分发 / skill / !shell / 跑一轮 chat（注入给 TuiApp）。

        替代旧 while-loop body。TuiApp 把本协程跑成 task、维护运行态、Ctrl-C→abort，故这里不再
        处理 SIGINT / 行读 / 取消计数。异常由 TuiApp._run_turn 吞（error_raised 事件已入 timeline）。"""
        inp = text.strip()
        # 中文输入法的全角斜杠／归一为半角，否则 "／memory" 不匹配命令、被当普通文本发给 AI。
        if inp[:1] == "／":
            inp = "/" + inp[1:]
        if not inp:
            return
        if inp in ("exit", "quit"):
            _app.request_exit()           # "Bye!" 在 _app.run() 返回后统一打印
            return

        # !<command>：用户直跑 shell（等同终端执行），不进 AI 对话、不走模型权限系统。
        if inp.startswith("!"):
            cmd = inp[1:].strip()
            if cmd:
                _say(await _host.current_thread.execute_user_shell(cmd))
            return

        # REPL slash 命令 —— 经 registry 分发（most-specific-first；非命令回退 skill/chat）。
        _result = await dispatch(inp, _REGISTRY, _host.context())
        if _result is not NOT_A_COMMAND:
            if isinstance(_result, Prompt):
                await _drive_turn(_result.prompt)
            elif isinstance(_result, Local):
                if _result.output:
                    _say(_result.output)
                if getattr(_result, "refresh_transcript", False):
                    _refresh_transcript()
                if getattr(_result, "prefill", None) is not None:
                    _app.input_buffer.text = _result.prefill or ""
                if _result.exit_repl:
                    _app.request_exit()
            elif isinstance(_result, Control):
                await _apply_control(_host, _result)
            return

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            try:
                invocation = _host.current_thread.invoke_skill(cmd_name, cmd_args)
            except Exception as e:
                if "abort" not in str(e).lower():
                    _err(str(e))
                return
            if invocation.handled:
                if invocation.notice:
                    _say(invocation.notice)
                if invocation.error:
                    _err(invocation.error)
                elif invocation.prompt:
                    await _drive_turn(invocation.prompt)
                return

        # Normal chat
        await _drive_turn(inp)

    _app.set_submit_handler(_submit)
    print_welcome()
    # docs/18：TuiApp（full_screen=False）接管输入主循环——取代 _async_read_line while-loop +
    # signal SIGINT handler（Ctrl-C 现由 app key binding 处理：运行中→abort、idle 空行双击→退）。
    try:
        await _app.run()
    finally:
        print("\nBye!\n")          # app 退出后终端已复原，plain print 干净（覆盖所有退出路径）
        # docs/14 SessionLease：REPL 退出 → 释放当前 thread 的会话写锁（rebind 已交接/关闭旧租约，
        # 故只需释放 current_thread 的那把，幂等）。
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
  -c, --continue      Continue the most recent session
  -r                  Browse and select from past sessions at startup
  --session ID|PATH   Use a specific managed session ID, partial ID, or session.jsonl path
  --fork ID|PATH      Fork a managed session into a new session and start there
  --name, -n NAME     Set the session display name at startup
  --no-session        Ephemeral mode; delete this run's generated session on exit
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --map-tokens N      Suggested tokens for repo map; use 0 to disable
  --map-refresh MODE  Repo map refresh: auto|always|files|manual (default: auto)
  --map-multiplier-no-files N
                      Repo map token multiplier before files are read (default: 2)
  --trajectory        Project the canonical session tree into a trajectory (analysis/RL lane)
  --trajectory-level {summary,full}
                      summary (default): drop heavy payloads, keep hash + summary.
                      full: keep full prompts/messages/tool results — may contain secrets.
  --verbose           Print per-turn token cost and MCP connection logs (default: quiet)
  --memory-backend B  Long-term memory: simplemem|markdown|off (default: markdown)
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
  nanocode -c
  nanocode -r
  nanocode --session <id>
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
    if args.map_tokens is not None:
        os.environ["NANOCODE_MAP_TOKENS"] = str(args.map_tokens)
    if args.map_refresh is not None:
        os.environ["NANOCODE_MAP_REFRESH"] = args.map_refresh
    if args.map_multiplier_no_files is not None:
        os.environ["NANOCODE_MAP_MULTIPLIER_NO_FILES"] = str(args.map_multiplier_no_files)

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
    # 交互且不信任 → 方向键确认（信任则记住并继续，否则退出）；非交互 → 隐式信任。
    from ..trust import ensure_workspace_trust
    _interactive = not bool(args.prompt) and sys.stdin.isatty()
    _choice_fn = None
    if _interactive and sys.stdout.isatty():
        from ..tui.choice_prompt import workspace_trust_choice
        _choice_fn = workspace_trust_choice
    workspace_trusted = ensure_workspace_trust(
        Path.cwd(), interactive=_interactive, choice_fn=_choice_fn)

    startup_modes = [
        bool(args.continue_session),
        bool(args.resume_picker),
        bool(args.session),
        bool(args.fork_session),
    ]
    if sum(1 for x in startup_modes if x) > 1:
        print_error("Choose only one of -c, -r, --session, or --fork.")
        sys.exit(2)
    if args.no_session and any(startup_modes):
        print_error("--no-session cannot be combined with -c, -r, --session, or --fork.")
        sys.exit(2)

    runtime = AgentRuntime()

    # Resolve startup session BEFORE constructing Agent (so we adopt the session_id).
    # canonical 树是唯一 resume 权威（docs/16 C-3：legacy flat/v2 发现面已删，latest 必有树）。
    adopt_sid = None
    if args.continue_session:
        adopt_sid = get_latest_session_id()
        if adopt_sid is None:
            print_info("No previous sessions found.")
    elif args.session:
        adopt_sid, err = _resolve_session_arg(args.session)
        if err:
            print_error(err)
            sys.exit(1)
    elif args.fork_session:
        source_sid, err = _resolve_session_arg(args.fork_session)
        if err:
            print_error(err)
            sys.exit(1)
        adopt_sid, err = runtime.startup_fork_session(source_sid)
        if err:
            print_error(err)
            sys.exit(1)
    elif args.resume_picker:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print_error("-r requires an interactive terminal. Use --session <id> in non-interactive mode.")
            sys.exit(2)
        try:
            adopt_sid = asyncio.run(_select_startup_session(str(Path.cwd().resolve())))
        except KeyboardInterrupt:
            sys.exit(130)
        if adopt_sid is None:
            print_info("No session selected.")
            sys.exit(0)

    # trajectory 采集（canonical 树的 DERIVED 投影 / RL 分析专用）：
    # 显式 flag 或 NANOCODE_TRAJECTORY 环境变量开启；level 非法/缺省退回 "summary"。
    traj_on = _trajectory_enabled(args.trajectory)
    traj_lvl = _trajectory_level(args.trajectory_level)

    cfg = AgentConfig(
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
        memory_backend_choice=args.memory_backend,
        cwd=str(Path.cwd()),
        sandbox_profile=args.sandbox_profile or "default",
    )

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

    from ..session.tree import SessionBusyError, SessionTreeError
    try:
        thread = runtime.thread_start(cfg)
    except SessionBusyError:
        sid = adopt_sid or "(new)"
        print_error(f"session '{sid}' is busy — another nanocode process holds its "
                    f"writer lock. Start a new session, or resume a different one.")
        sys.exit(1)
    except SessionTreeError as e:
        print_error(f"corrupt session tree: {e}")
        sys.exit(1)
    if adopt_sid is not None:
        print_info(f"Session resumed: {adopt_sid} ({len(thread.messages())} messages).")
    if args.session_name is not None:
        try:
            thread.set_session_name(args.session_name)
        except RuntimeError as e:
            print_error(str(e))
            sys.exit(1)
    for diag in getattr(thread.services, "diagnostics", ()):
        print_info(diag)

    prompt = " ".join(args.prompt) if args.prompt else None

    def _finish_session() -> None:
        if args.no_session:
            _cleanup_ephemeral_session(thread.session_id)

    if args.rpc:
        # Headless RPC mode（docs/17 Phase 5b）：JSON-lines over stdio 驱动同一 session,无 TUI。
        from .rpc import run_rpc_mode
        try:
            asyncio.run(run_rpc_mode(RuntimeHost(runtime, thread, registry=_REGISTRY, interactive=False)))
        finally:
            thread.release_lease()
            _finish_session()
    elif prompt:
        # One-shot mode —— docs/15 Phase 7：headless 路径同样**仅**经 RuntimeThread.run,不绕过 runtime
        # （逃生阀已删）。

        async def _one_shot() -> None:
            from .terminal_client import TerminalClient
            thread.subscribe(TerminalClient().on_event)   # docs/17 Phase 1：headless 也经订阅客户端渲染
            await thread.run(prompt)

        try:
            asyncio.run(_one_shot())
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
        finally:
            thread.release_lease()
            _finish_session()
    else:
        # Interactive REPL
        try:
            asyncio.run(run_repl(thread))
        finally:
            _finish_session()                  # run_repl 退出时已 release 当前 thread 的 lease


if __name__ == "__main__":
    main()
