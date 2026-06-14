"""tui/rich_app.py —— Rich Live 交互客户端（docs/18：Codex inline-viewport 模型）。

取代 prompt_toolkit `TuiApp`。一个 `Live(console=tui.console, screen=False)` 在底部持有重绘区
（footer + 输入行 + thinking spinner + 流式助手；modal；session 选择器面板），**自动刷新**→
thinking 动画 + 流式丝滑。已完成的 transcript 经 `tui.console.print` 落到 Live 之上、滚进原生
scrollback（= Codex insert_history）——Live 绑定了该 console，故无需 run_in_terminal、不再闪。

嵌入式边界：本模块只 import `.{state,reducer,footer,selector,line_editor,primitives}` 与 rich；
**零** import agent/session/tools。消费的是 `on_event(AgentEvent 信封)` + 注入的
`RuntimeThread`/`ApprovalManager` 公开面，与旧 `TuiApp` 同一契约（drop-in）。
"""

from __future__ import annotations

import asyncio
import os
import sys

from rich.console import Group
from rich.panel import Panel
from rich.padding import Padding
from rich.spinner import Spinner
from rich.text import Text
from rich import box as _box

from .. import tui as _tui
from .line_editor import KeyParser, LineEditor, PasteToken, raw_mode, restore
from .reducer import hydrate_status, reduce
from .selector import Outcome
from .state import ApprovalModal, AssistantItem, PlanModal, SelectorState, SessionBoundaryItem, ThinkingItem, TuiState

_ACCENT = "\x1b[36m"
_RESET = "\x1b[0m"


def _ansi_lines(s: str) -> list[Text]:
    return [Text.from_ansi(line) for line in s.split("\n")]


def _as_str(x) -> str:
    return x.value if hasattr(x, "value") else ("" if x is None else str(x))


class _InputProxy:
    """暴露 `.text` 给 cli（fork 预填 `_app.input_buffer.text = prefill`）。"""

    def __init__(self, editor: LineEditor) -> None:
        self._editor = editor

    @property
    def text(self) -> str:
        return self._editor.text

    @text.setter
    def text(self, value: str) -> None:
        self._editor.set_text(value or "")


class RichApp:
    """挂在 RuntimeThread 上的 Rich Live 客户端（与 TuiApp 同一 client 协议）。"""

    def __init__(self, *, render_event=None, input=None, output=None,
                 completer=None, history=None, auto_suggest=None) -> None:
        self.state = TuiState()
        self.thread = None
        self._render_event = render_event
        self._on_submit = None
        self._console = output if output is not None else _tui.console
        self._input = input                      # None=stdin；否则 fd / 有 fileno() 的对象（测试 pipe）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit: asyncio.Future | None = None
        self._unsubscribe = None
        self._turn_task: asyncio.Task | None = None
        self._approval_future: asyncio.Future | None = None
        self._plan_future: asyncio.Future | None = None
        self._selector_future: asyncio.Future | None = None
        self._ask_future: asyncio.Future | None = None
        self._cancel_count = 0
        self._parser = KeyParser()
        self._esc_timer = None
        self._editor = LineEditor(history=self._load_history())
        self.input_buffer = _InputProxy(self._editor)
        self._spinner = Spinner("dots", text=Text(" Thinking…", style="dim"), style="cyan")
        self._live = None

    # ── history（自管，不依赖 prompt_toolkit）─────────────────────────
    def _load_history(self) -> list[str]:
        try:
            from ..paths import history_file
            p = history_file()
            if os.path.exists(p):
                return [ln.rstrip("\n") for ln in open(p, encoding="utf-8") if ln.strip()][-500:]
        except Exception:
            pass
        return []

    def _save_history(self, line: str) -> None:
        try:
            from ..paths import history_file
            with open(history_file(), "a", encoding="utf-8") as f:
                f.write(line.replace("\n", " ") + "\n")
        except Exception:
            pass

    # ── client 协议：与 thread 绑定 ──────────────────────────────────
    def bind_thread(self, thread) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        self.thread = thread
        if thread is not None:
            try:
                hydrate_status(self.state, thread.state())
            except Exception:
                pass
            sid = self.state.status.session_id or getattr(thread, "session_id", "")
            self.state.timeline.append(SessionBoundaryItem(session_id=sid))
            self._unsubscribe = thread.subscribe(self.on_event)
        self._refresh()

    def on_event(self, env: dict) -> None:
        loop = self._loop
        if loop is None:
            self._apply(env)
        else:
            loop.call_soon_threadsafe(self._apply, env)

    def _apply(self, env: dict) -> None:
        reduce(self.state, env)
        if self._render_event is not None:
            try:
                self._render_event(env)   # transcript → tui.console → Live 之上（scrollback）
            except Exception:
                pass
        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            try:
                self._live.refresh()
            except Exception:
                pass

    def set_submit_handler(self, fn) -> None:
        self._on_submit = fn

    def print_above(self, text: str, *, error: bool = False) -> None:
        style = "red" if error else "cyan"
        self._console.print(Text(str(text), style=style if error else None))

    def request_exit(self) -> None:
        if self._exit is not None and not self._exit.done():
            self._exit.set_result(None)

    # ── 审批 / plan（注入到 ApprovalManager）─────────────────────────
    async def confirm_fn(self, message: str, command: str = "") -> bool:
        loop = self._loop or asyncio.get_event_loop()
        fut = loop.create_future()
        self._approval_future = fut
        if self.state.modal is None:
            self.state.modal = ApprovalModal(command=command, message=message)
        self.state.mode = "approval"
        self._refresh()
        try:
            return await fut
        finally:
            self._approval_future = None
            self.state.modal = None
            self.state.mode = "running" if self._is_running() else "idle"
            self._refresh()

    _PLAN_CHOICES = {
        "1": {"choice": "clear-and-execute"}, "2": {"choice": "execute"},
        "3": {"choice": "manual-execute"}, "4": {"choice": "keep-planning", "feedback": None},
    }

    async def plan_approval_fn(self, plan_content: str) -> dict:
        loop = self._loop
        if loop is None:
            return {"choice": "manual-execute"}
        fut = loop.create_future()
        self._plan_future = fut
        self.state.mode = "plan_approval"
        self.state.plan_modal = PlanModal(plan_content=plan_content)
        self._refresh()
        try:
            return await fut
        finally:
            self._plan_future = None
            self.state.plan_modal = None
            self.state.mode = "running" if self._is_running() else "idle"
            self._refresh()

    # ── 选择器 / 文本输入（owner 协议）──────────────────────────────
    async def run_selector(self, model, *, initial_index: int = 0):
        loop = self._loop
        if loop is None:
            return Outcome("cancel", index=initial_index)
        items = model.items()
        idx = 0 if not items else max(0, min(initial_index, len(items) - 1))
        self.state.selector = SelectorState(model=model, index=idx)
        self.state.mode = "selector"
        self._refresh()
        fut = loop.create_future()
        self._selector_future = fut
        try:
            return await fut
        finally:
            self._selector_future = None
            self.state.selector = None
            self.state.mode = "running" if self._is_running() else "idle"
            self._refresh()

    async def ask_text(self, prompt: str):
        loop = self._loop
        if loop is None:
            return None
        self.state.text_prompt = prompt
        self.state.mode = "ask_text"
        self._editor.reset()
        self._refresh()
        fut = loop.create_future()
        self._ask_future = fut
        try:
            return await fut
        finally:
            self._ask_future = None
            self.state.text_prompt = None
            self._editor.reset()
            self.state.mode = "running" if self._is_running() else "idle"
            self._refresh()

    # ── 运行态 / 提交 ──────────────────────────────────────────────
    def _is_running(self) -> bool:
        if self.thread is not None and getattr(self.thread, "is_processing", False):
            return True
        return self._turn_task is not None and not self._turn_task.done()

    def _submit(self, text: str) -> None:
        if not text.strip() or self.thread is None or self._is_running():
            return
        self.state.mode = "running"
        self._turn_task = asyncio.ensure_future(self._run_turn(text))
        self._refresh()

    async def _run_turn(self, text: str) -> None:
        try:
            if self._on_submit is not None:
                await self._on_submit(text)
            else:
                await self.thread.run(text)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            if self.state.mode == "running":
                self.state.mode = "idle"
            self._refresh()

    # ── 输入分发（替代 prompt_toolkit key bindings）──────────────────
    def feed(self, data: bytes) -> None:
        """把原始字节喂进解析器并路由（生产由 add_reader 调；测试可直接调）。"""
        for tok in self._parser.feed(data):
            self._dispatch(tok)
        self._refresh()

    def _read_ready(self, fd: int) -> None:
        try:
            data = os.read(fd, 4096)
        except OSError:
            data = b""
        if not data:                       # EOF
            self.request_exit()
            return
        self.feed(data)
        # 裸 ESC（Escape 键 vs 转义序列起头）超时裁决：~40ms 无后续字节则当 Escape。
        if self._parser.pending_is_escape() and self._loop is not None:
            if self._esc_timer is not None:
                self._esc_timer.cancel()
            self._esc_timer = self._loop.call_later(0.04, self._flush_escape)

    def _flush_escape(self) -> None:
        self._esc_timer = None
        for tok in self._parser.flush_escape():
            self._dispatch(tok)
        self._refresh()

    def _dispatch(self, tok) -> None:
        if self.state.modal is not None:
            self._dispatch_approval(tok)
        elif self.state.plan_modal is not None:
            self._dispatch_plan(tok)
        elif self.state.selector is not None:
            self._dispatch_selector(tok)
        elif self.state.text_prompt is not None:
            self._dispatch_ask(tok)
        else:
            self._dispatch_main(tok)

    def _dispatch_approval(self, tok) -> None:
        if tok == "y":
            self._resolve(self._approval_future, True)
        elif tok in ("n", "escape", "ctrl-c"):
            self._resolve(self._approval_future, False)

    def _dispatch_plan(self, tok) -> None:
        if tok in self._PLAN_CHOICES:
            self._resolve(self._plan_future, dict(self._PLAN_CHOICES[tok]))
        elif tok in ("escape", "ctrl-c"):
            self._resolve(self._plan_future, dict(self._PLAN_CHOICES["4"]))

    def _dispatch_ask(self, tok) -> None:
        if tok in ("escape", "ctrl-c"):
            self._resolve(self._ask_future, None)
            return
        action = self._editor.handle(tok)
        if action == "submit":
            text = self._editor.text
            self._editor.reset()
            self._resolve(self._ask_future, text)

    def _dispatch_main(self, tok) -> None:
        action = self._editor.handle(tok)
        if action == "submit":
            text = self._editor.text
            self._editor.reset()
            self._cancel_count = 0
            if text.strip():
                self._editor.add_history(text)
                self._save_history(text)
                self._echo_user(text)        # 把用户消息印进 scrollback（开口 You 框），不再消失
            self._submit(text)
        elif action == "cancel":              # Ctrl-C
            if self._is_running():
                self._cancel_count = 0
                try:
                    self.thread.abort()
                except Exception:
                    pass
            elif self._editor.text:
                self._editor.reset()
                self._cancel_count = 0
            else:
                self._cancel_count += 1
                if self._cancel_count >= 2:
                    self.request_exit()
        elif action == "eof":                 # Ctrl-D on empty
            self.request_exit()

    # ── 选择器按键路由 ──────────────────────────────────────────────
    def _sel_current(self):
        s = self.state.selector
        items = s.model.items() if s else []
        return items[s.index] if s and items and 0 <= s.index < len(items) else None

    def _sel_move(self, delta: int) -> None:
        s = self.state.selector
        if s is None or s.model.confirming():
            return
        items = s.model.items()
        if items:
            s.index = max(0, min(s.index + delta, len(items) - 1))

    def _sel_clamp(self) -> None:
        s = self.state.selector
        if s is None:
            return
        n = len(s.model.items())
        s.index = 0 if n == 0 else max(0, min(s.index, n - 1))

    def _dispatch_selector(self, tok) -> None:
        s = self.state.selector
        if s is None:
            return
        m = s.model
        query = m.supports_query()
        if m.confirming():
            if tok == "enter":
                self._apply_keyresult(m.on_key("confirm", self._sel_current(), s.index), "confirm")
            elif tok in ("escape", "ctrl-c"):
                self._apply_keyresult(m.on_key("abort", self._sel_current(), s.index), "abort")
            return
        if tok == "up":
            self._sel_move(-1)
        elif tok == "down":
            self._sel_move(1)
        elif tok == "pageup":
            self._sel_move(-max(1, m.max_visible(self._term_h())))
        elif tok == "pagedown":
            self._sel_move(max(1, m.max_visible(self._term_h())))
        elif tok == "enter":
            if self._sel_current() is not None:
                self._resolve(self._selector_future, Outcome("done", item=self._sel_current(), index=s.index))
        elif tok == "escape" or (tok == "ctrl-c"):
            self._resolve(self._selector_future, Outcome("cancel", index=s.index))
        elif tok in ("k",) and not query:
            self._sel_move(-1)
        elif tok in ("j",) and not query:
            self._sel_move(1)
        elif tok in ("q",) and not query:
            self._resolve(self._selector_future, Outcome("cancel", index=s.index))
        elif tok == "backspace":
            if query and m.query():
                m.set_query(m.query()[:-1])
                self._sel_clamp()
        elif tok in m.extra_keys():
            self._apply_keyresult(m.on_key(tok, self._sel_current(), s.index), tok)
        elif isinstance(tok, str) and len(tok) == 1 and tok.isprintable() and query:
            m.set_query(m.query() + tok)
            self._sel_clamp()
        elif isinstance(tok, PasteToken) and query:
            m.set_query(m.query() + tok.text.replace("\n", " "))
            self._sel_clamp()

    def _apply_keyresult(self, r, key: str) -> None:
        if r is None:
            return
        s = self.state.selector
        if r.clipboard_text is not None:
            self._copy_clipboard(r.clipboard_text)
        if r.kind == "continue":
            return
        if r.kind == "refresh":
            self._sel_clamp()
        elif r.kind == "done":
            self._resolve(self._selector_future,
                          Outcome("done", item=r.result if r.result is not None else self._sel_current(),
                                  index=s.index if s else 0))
        elif r.kind == "cancel":
            self._resolve(self._selector_future, Outcome("cancel", index=s.index if s else 0))
        elif r.kind == "edit":
            self._resolve(self._selector_future,
                          Outcome("edit", item=self._sel_current(), edit_action=r.edit_action or key,
                                  index=s.index if s else 0))

    @staticmethod
    def _copy_clipboard(text: str) -> None:
        try:
            import shutil
            import subprocess
            for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, input=text.encode(), timeout=5)
                    return
        except Exception:
            pass

    @staticmethod
    def _resolve(fut, value) -> None:
        if fut is not None and not fut.done():
            fut.set_result(value)

    # ── 渲染（Live 底部区域）────────────────────────────────────────
    def _term_h(self) -> int:
        try:
            return self._console.size.height
        except Exception:
            return 30

    def _term_w(self) -> int:
        try:
            return self._console.size.width
        except Exception:
            return 100

    def __rich__(self):
        """Live 每帧调用——读当前 state 返回底部区域（动画/流式靠此重渲）。"""
        st = self.state
        if st.selector is not None:
            return self._render_selector()
        if st.modal is not None:
            m = st.modal
            body = m.message + (f"\n  {m.command}" if m.command else "")
            return Panel(Text.from_ansi(f"{body}\n\n  y allow once   n deny"),
                         title="Approval", border_style="yellow")
        if st.plan_modal is not None:
            return Panel(Text(f"{st.plan_modal.plan_content}\n\n"
                              "  1 clear+execute   2 execute keep   3 manual-approve   4 keep planning"),
                         title="Plan Approval", border_style="cyan")
        parts = []
        if st.text_prompt is not None:
            parts.append(Text(st.text_prompt, style="dim"))
            parts.append(self._editor.render(self._term_w(), prompt=""))
            return Group(*parts)
        # normal: 流式助手(⏺) + thinking spinner + 开口输入框 + footer（col2 左轨）
        streaming = self._open_assistant_text()
        if streaming:
            parts.append(Text.assemble(("⏺ ", "cyan")) + Text(streaming))
        if st.mode == "running" and not streaming:
            parts.append(self._spinner)
        parts.append(Text(""))               # 输入框前空一行（Codex 组间留空）
        parts.append(self._input_frame())
        foot = self._status_line()
        if foot is not None:
            parts.append(Padding(foot, (0, 0, 0, 2)))   # FOOTER_INDENT_COLS=2
        return Group(*parts)

    # ── 开口框（HORIZONTALS：上下横线、左右开口；V1 设计）──────────────────────
    @staticmethod
    def _open_frame(body, *, title=None, subtitle=None, border="cyan"):
        return Panel(body, box=_box.HORIZONTALS, border_style=border, padding=0,
                     title=title, subtitle=subtitle, title_align="left", subtitle_align="right")

    def _input_frame(self):
        """底部输入开口框：标题 nanocode，副标题 model · ctx%，正文 `> ` gutter（文本落 col2）。"""
        body = self._editor.render(self._term_w())   # "> text" + 光标块
        sub = None
        if self.thread is not None:
            try:
                s = self.thread.status()
                model = s.get("model", "")
                win = s.get("context_window", 0) or 0
                used = s.get("input_tokens", 0) or 0
                pct = f"{used / win * 100:.0f}%" if win else ""
                sub = "[dim]" + " · ".join(x for x in (model, pct) if x) + "[/dim]"
            except Exception:
                sub = None
        return self._open_frame(body, title="[cyan]nanocode[/]", subtitle=sub, border="cyan")

    def _status_line(self):
        """footer 单行（col2）：cwd (branch) · ↑in ↓out · $cost。model/ctx% 在输入框副标题。"""
        if self.thread is None:
            return None
        try:
            from .footer import format_tokens, format_cwd, git_branch
            s = self.thread.status()
            cwd = s.get("cwd", "")
            pwd = format_cwd(cwd, os.path.expanduser("~"))
            br = git_branch(cwd)
            if br:
                pwd = f"{pwd}  ({br})"
            parts = [pwd]
            it, ot = s.get("input_tokens", 0), s.get("output_tokens", 0)
            if it or ot:
                parts.append(f"↑{format_tokens(it)} ↓{format_tokens(ot)}")
            cost = s.get("cost_usd")
            if cost:
                parts.append(f"${cost:.3f}")
            return Text("  ·  ".join(parts), style="dim")
        except Exception:
            return None

    def _echo_user(self, text: str) -> None:
        """把用户消息印到 scrollback：开口 You 框（与 V1 mockup 一致）。"""
        try:
            frame = self._open_frame(Padding(Text(text.strip()), (0, 0, 0, 1)),
                                     title="[grey62]You[/]", border="grey42")
            self._console.print(frame)
            self._console.print("")          # 组后空一行
        except Exception:
            pass

    def _open_assistant_text(self) -> str:
        for it in reversed(self.state.timeline):
            if isinstance(it, (AssistantItem, ThinkingItem)):
                if not it.complete and it.text:
                    return it.text if isinstance(it, AssistantItem) else ""
                return ""
        return ""

    def _render_selector(self):
        s = self.state.selector
        m = s.model
        w, h = self._term_w(), self._term_h()
        lines: list = [Text.from_ansi(f"{_ACCENT}{'─' * w}{_RESET}")]
        for hl in m.header_lines(w):
            lines += _ansi_lines(_as_str(hl))
        sl = m.search_line(w)
        if sl is not None:
            lines += _ansi_lines(_as_str(sl))
        lines.append(Text(""))
        items = m.items()
        if not items:
            lines.append(Text("  (no entries)", style="dim"))
        else:
            n = len(items)
            idx = max(0, min(s.index, n - 1))
            mv = max(3, m.max_visible(h))
            start = max(0, min(idx - mv // 2, max(0, n - mv)))
            for i in range(start, min(start + mv, n)):
                lines += _ansi_lines(_as_str(m.list_text(items[i], i == idx, w)))
            lines.append(Text.from_ansi(f"\x1b[2m  ({idx + 1}/{n}){m.status_suffix()}{_RESET}"))
        lines.append(Text.from_ansi(f"{_ACCENT}{'─' * w}{_RESET}"))
        return Group(*lines)

    # ── 运行 ────────────────────────────────────────────────────────
    def _resolve_fd(self):
        if self._input is None:
            try:
                return sys.stdin.fileno()
            except Exception:
                return None
        if isinstance(self._input, int):
            return self._input
        if hasattr(self._input, "fileno"):
            try:
                return self._input.fileno()
            except Exception:
                return None
        return None

    async def run(self, *, patch: bool = True) -> None:
        from rich.live import Live

        self._loop = asyncio.get_running_loop()
        self._exit = self._loop.create_future()
        fd = self._resolve_fd()
        is_tty = fd is not None and os.isatty(fd)
        saved = raw_mode(fd) if (self._input is None and is_tty) else None
        if is_tty:
            sys.stdout.write("\x1b[?2004h"); sys.stdout.flush()   # bracketed paste
        if fd is not None:
            try:
                self._loop.add_reader(fd, self._read_ready, fd)
            except (NotImplementedError, OSError):
                fd = None
        try:
            with Live(self, console=self._console, screen=False, refresh_per_second=20,
                      auto_refresh=True, transient=False) as live:
                self._live = live
                try:
                    await self._exit
                except (KeyboardInterrupt, asyncio.CancelledError):
                    # 兜底：ISIG 已关、键盘 Ctrl-C 走字节路径；但仍可能有 stray SIGINT（kill -INT /
                    # raw mode 生效前的窗口）。运行中→优雅 abort 当前 turn；否则当退出。绝不抛栈。
                    if self._is_running():
                        try:
                            self.thread.abort()
                        except Exception:
                            pass
        finally:
            self._live = None
            if fd is not None:
                try:
                    self._loop.remove_reader(fd)
                except Exception:
                    pass
            if is_tty:
                sys.stdout.write("\x1b[?2004l"); sys.stdout.flush()
            if saved is not None:
                restore(fd, saved)
