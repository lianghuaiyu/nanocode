"""tui/app.py —— prompt_toolkit 交互 app（docs/18 step 2）。

`full_screen=False`（inline，非 alt-screen）：transcript 继续走原生终端 scrollback（保留复制粘贴/
滚动，同 Claude Code/aider），本 app 只在底部持有 **footer + 输入区 + modal Float** 重绘区。
core 只 `emit(AgentEvent)`，本 app 是订阅端：`on_event` 把事件归约进 `TuiState`（reducer）并
`invalidate()`，**绝不阻塞、绝不在订阅腿里渲染 transcript**（transcript 由 TerminalClient 印到
scrollback，与本 app 并存）。

线程纪律：`Agent.emit` 的扇出腿可能在 worker 线程（tool exec / stream）里调 `on_event`，故全部
经 `loop.call_soon_threadsafe` marsh 回事件循环线程，保证 state 变更与 invalidate 都在 UI 线程、
且保 FIFO 顺序。

输入：Enter 提交、Ctrl-J / Meta-Enter 换行。提交 → `create_task(thread.run)`（不在 key handler 里
await 长任务）。Ctrl-C：turn 运行中→`thread.abort()`（优雅取消，turn_aborted 事件回到 idle，app 不退）；
idle 连按两次→退出。Ctrl-D→退出。审批：注入的 `confirm_fn` 开 modal Float 并 await future，y/n 解决它。
"""

from __future__ import annotations

import asyncio
import os
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import Frame

from .reducer import hydrate_status, reduce
from .state import ApprovalModal, PlanModal, SessionBoundaryItem, TuiState


class TuiApp:
    """挂在 RuntimeThread 上的交互 app（订阅事件 → TuiState → 重绘 footer/modal/输入）。

    领域逻辑注入（保持 app 与具体命令栈解耦）：
    - `on_submit(text)`：async；用户提交一行时调（client 在此做命令分发 / skill / !shell / 跑 turn）。
      缺省退回 `thread.run`（纯 chat）。app 负责把它跑成 task、维护运行态、不在 key handler 里 await。
    - `render_event(env)`：每条订阅事件**额外**调（client 用它把 transcript 印到 scrollback，
      与 footer/modal 的 reduce 并行）。
    """

    def __init__(self, *, input=None, output=None, on_submit=None, render_event=None,
                 completer=None, history=None, auto_suggest=None) -> None:
        self.state = TuiState()
        self.thread = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe = None
        self._turn_task: asyncio.Task | None = None
        self._approval_future: asyncio.Future | None = None
        self._plan_future: asyncio.Future | None = None
        self._selector_future: asyncio.Future | None = None
        self._ask_future: asyncio.Future | None = None
        self._cancel_count = 0  # idle 连按 Ctrl-C 退出计数
        self._on_submit = on_submit
        self._render_event = render_event

        # 输入框：持久历史（↑↓ 回溯）+ 命令补全 + auto-suggest——与旧 PromptSession REPL 平价
        # （docs/18 step 6：cutover 后补回，避免回归）。complete_while_typing 触发补全菜单 Float。
        self.input_buffer = Buffer(
            multiline=True, completer=completer, complete_while_typing=completer is not None,
            history=history, auto_suggest=auto_suggest,
        )
        self._app = self._build_app(input=input, output=output)

    # ── 与 thread 绑定（client 协议；host 在 thread 替换时调）─────────────────
    def bind_thread(self, thread) -> None:
        """订阅新 thread 的事件流，从快照 hydrate footer，插入 session 边界。

        旧 thread 随 dispose 丢弃其 _listeners，故无需显式 unsubscribe；仍保守地解绑已知句柄。"""
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
        self._safe_invalidate()

    # ── 事件订阅（可能从 worker 线程进来；全部 marsh 回 UI 线程）──────────────
    def on_event(self, env: dict) -> None:
        loop = self._loop
        if loop is None:
            self._apply(env)  # app 未跑（测试 / 早期事件）：直接应用
        else:
            loop.call_soon_threadsafe(self._apply, env)

    def _apply(self, env: dict) -> None:
        reduce(self.state, env)
        if self._render_event is not None:
            # transcript → terminal scrollback **above** the app via run_in_terminal（协调
            # erase→print→repaint）；直接 print 会被 app 连续重绘覆盖（尤其无 CPR 的终端光标跟踪失准）。
            self._emit_above(lambda: self._render_event(env))
        self._safe_invalidate()

    def _emit_above(self, fn) -> None:
        """在 app 渲染区**之上**输出（run_in_terminal 挂起 app→运行 fn→重绘）。无 loop 则直跑。"""
        loop = self._loop
        if loop is None:
            try:
                fn()
            except Exception:
                pass
            return
        from prompt_toolkit.application import run_in_terminal

        async def _run():
            try:
                await run_in_terminal(fn)
            except Exception:
                pass

        asyncio.ensure_future(_run())

    def print_above(self, text: str, *, error: bool = False) -> None:
        """client（命令输出 / 通知）经此把文本印到 app 之上（同 transcript 路径，避免被重绘覆盖）。"""
        from .. import tui as _tui
        self._emit_above(lambda: (_tui.print_error if error else _tui.print_info)(text))

    def _safe_invalidate(self) -> None:
        try:
            self._app.invalidate()
        except Exception:
            pass

    def set_submit_handler(self, fn) -> None:
        """注入 client 的 async submit handler（构造期循环依赖：app↔host↔handler，故事后注入）。"""
        self._on_submit = fn

    # ── 审批（注入到 ApprovalManager 的 confirm_fn）────────────────────────────
    async def confirm_fn(self, message: str, command: str = "") -> bool:
        """开 modal Float 并挂起，等 y/n 键解决。无运行 loop 时 fail-closed deny。

        modal 优先由 `approval_requested` 事件经 reduce 显示（携真实 command）；此处仅在事件未先到时
        兜底建一个，避免覆盖事件已设的 command。"""
        loop = self._loop or asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._approval_future = fut
        if self.state.modal is None:
            self.state.modal = ApprovalModal(command=command, message=message)
        self.state.mode = "approval"
        self._safe_invalidate()
        try:
            return await fut
        finally:
            self._approval_future = None
            self.state.modal = None
            self.state.mode = "running" if self._is_running() else "idle"
            self._safe_invalidate()

    def _resolve_approval(self, approved: bool) -> None:
        fut = self._approval_future
        if fut is not None and not fut.done():
            fut.set_result(approved)

    # ── plan 审批（注入到 ApprovalManager 的 plan_approval_fn）──────────────────
    _PLAN_CHOICES = {
        "1": {"choice": "clear-and-execute"},
        "2": {"choice": "execute"},
        "3": {"choice": "manual-execute"},
        "4": {"choice": "keep-planning", "feedback": None},  # feedback-via-input 属后续步骤
    }

    async def plan_approval_fn(self, plan_content: str) -> dict:
        """开 plan modal 并挂起，等 1-4 键解决。无运行 loop → 默认 manual-execute（fail-safe）。"""
        loop = self._loop
        if loop is None:
            return {"choice": "manual-execute"}
        fut: asyncio.Future = loop.create_future()
        self._plan_future = fut
        self.state.mode = "plan_approval"
        self.state.plan_modal = PlanModal(plan_content=plan_content)
        self._safe_invalidate()
        try:
            return await fut
        finally:
            self._plan_future = None
            self.state.plan_modal = None
            self.state.mode = "running" if self._is_running() else "idle"
            self._safe_invalidate()

    def _resolve_plan(self, choice: str) -> None:
        fut = self._plan_future
        if fut is not None and not fut.done():
            fut.set_result(dict(self._PLAN_CHOICES[choice]))

    def request_exit(self) -> None:
        """client（submit handler 命中 exit/quit/Local.exit_repl）请求退出 app。"""
        try:
            self._app.exit()
        except Exception:
            pass

    # ── SelectorHost：in-app overlay 选择器 + 文本输入（docs/18 step 4）─────────────
    # owner（tui.session_pages.*）经注入的 host 调这两个 async 方法，取代旧
    # selector.py 独立 Application + ask_text 回调。对齐 Pi `ui.showOverlay()` 模型。

    async def run_selector(self, model, *, initial_index: int = 0):
        """把 SelectorModel 渲染成 app 内 overlay 区域，路由按键，返回 Outcome。无 loop → cancel。"""
        from .selector import Outcome
        loop = self._loop
        if loop is None:
            return Outcome("cancel", index=initial_index)
        self._clamp_selector(model, initial_index)
        from .state import SelectorState
        self.state.selector = SelectorState(model=model, index=self._sel_clamped)
        self.state.mode = "selector"
        try:
            self._app.layout.focus(self._sel_window)
        except Exception:
            pass
        self._safe_invalidate()
        fut: asyncio.Future = loop.create_future()
        self._selector_future = fut
        try:
            return await fut
        finally:
            self._selector_future = None
            self.state.selector = None
            self.state.mode = "running" if self._is_running() else "idle"
            try:
                self._app.layout.focus(self._input_window)
            except Exception:
                pass
            self._safe_invalidate()

    async def ask_text(self, prompt: str):
        """选择器内文本输入（rename / label）。复用主输入框（selector 此刻已关），Enter 提交、Esc/取消→None。"""
        loop = self._loop
        if loop is None:
            return None
        self.state.text_prompt = prompt
        self.state.mode = "ask_text"
        self.input_buffer.reset()
        self._safe_invalidate()
        fut: asyncio.Future = loop.create_future()
        self._ask_future = fut
        try:
            return await fut
        finally:
            self._ask_future = None
            self.state.text_prompt = None
            self.input_buffer.reset()
            self.state.mode = "running" if self._is_running() else "idle"
            self._safe_invalidate()

    def _clamp_selector(self, model, index: int) -> None:
        items = model.items()
        self._sel_clamped = 0 if not items else max(0, min(index, len(items) - 1))

    def _sel_move(self, delta: int) -> None:
        s = self.state.selector
        if s is None or s.model.confirming():
            return
        items = s.model.items()
        if items:
            s.index = max(0, min(s.index + delta, len(items) - 1))
        self._safe_invalidate()

    def _sel_page(self) -> int:
        _, height = self._sel_size()
        s = self.state.selector
        return max(1, s.model.max_visible(height)) if s is not None else 5

    def _sel_current(self):
        s = self.state.selector
        if s is None:
            return None
        items = s.model.items()
        return items[s.index] if items and 0 <= s.index < len(items) else None

    def _resolve_selector(self, outcome) -> None:
        fut = self._selector_future
        if fut is not None and not fut.done():
            s = self.state.selector
            if s is not None:
                outcome.index = s.index
            fut.set_result(outcome)

    def _sel_key(self, key: str) -> None:
        """Route a selector extra-key through the owner model (or into query text)."""
        s = self.state.selector
        if s is None:
            return
        if key not in s.model.extra_keys():
            if len(key) == 1 and key.isprintable() and s.model.supports_query():
                self._sel_query_input(key)
            return
        self._apply_keyresult(s.model.on_key(key, self._sel_current(), s.index), key)

    def _sel_dispatch(self, key: str) -> None:
        """Route a key straight to the model regardless of extra_keys (confirm enter/abort)."""
        s = self.state.selector
        if s is not None:
            self._apply_keyresult(s.model.on_key(key, self._sel_current(), s.index), key)

    def _apply_keyresult(self, r, key: str) -> None:
        from .selector import Outcome
        if r is None:
            return
        if r.clipboard_text is not None:
            try:
                self._app.clipboard.set_text(r.clipboard_text)
            except Exception:
                pass
        s = self.state.selector
        if r.kind == "continue":
            self._safe_invalidate()
            return
        if r.kind == "refresh":
            if s is not None:
                self._clamp_selector(s.model, s.index)
                s.index = self._sel_clamped
            self._safe_invalidate()
        elif r.kind == "done":
            self._resolve_selector(Outcome("done", item=r.result if r.result is not None else self._sel_current()))
        elif r.kind == "cancel":
            self._resolve_selector(Outcome("cancel"))
        elif r.kind == "edit":
            self._resolve_selector(Outcome("edit", item=self._sel_current(), edit_action=r.edit_action or key))

    def _sel_query_input(self, text: str) -> None:
        s = self.state.selector
        if s is None or s.model.confirming() or not s.model.supports_query():
            return
        s.model.set_query(s.model.query() + text)
        self._clamp_selector(s.model, s.index)
        s.index = self._sel_clamped
        self._safe_invalidate()

    def _sel_query_backspace(self) -> None:
        s = self.state.selector
        if s is None or not s.model.supports_query():
            return
        query = s.model.query()
        if not query:
            return
        s.model.set_query(query[:-1])
        self._clamp_selector(s.model, s.index)
        s.index = self._sel_clamped
        self._safe_invalidate()

    # ── 运行态 ────────────────────────────────────────────────────────────────
    def _is_running(self) -> bool:
        if self.thread is not None and getattr(self.thread, "is_processing", False):
            return True
        return self._turn_task is not None and not self._turn_task.done()

    def _submit(self, text: str) -> None:
        if not text.strip() or self.thread is None:
            return
        if self._is_running():
            return  # REPL 串行：运行中不接新提交
        self.state.mode = "running"
        self._turn_task = asyncio.ensure_future(self._run_turn(text))
        self._safe_invalidate()

    async def _run_turn(self, text: str) -> None:
        try:
            if self._on_submit is not None:
                await self._on_submit(text)  # client 领域逻辑：命令分发 / skill / !shell / 跑 turn
            else:
                await self.thread.run(text)  # 缺省：纯 chat
        except asyncio.CancelledError:
            pass  # abort 路径：turn_aborted 事件已把 mode 收回 idle
        except Exception:
            pass  # error_raised 事件已入 timeline；不外泄崩溃 app
        finally:
            if self.state.mode == "running":
                self.state.mode = "idle"
            self._safe_invalidate()

    # ── 布局 / 键位 ───────────────────────────────────────────────────────────
    def _footer_fragments(self):
        """从 thread.status() 现组 Pi 两行 footer（ANSI）；失败返回空（绝不拖垮重绘）。"""
        if self.thread is None:
            return ANSI("")
        try:
            from .footer import FooterState, git_branch, render_footer

            st = self.thread.status()
            cwd = st["cwd"]
            fs = FooterState(
                cwd=cwd, home=os.path.expanduser("~"), branch=git_branch(cwd),
                session_name=st.get("session_name"),
                input_tokens=st.get("input_tokens", 0), output_tokens=st.get("output_tokens", 0),
                cost_usd=st.get("cost_usd") or 0.0, context_used=st.get("input_tokens", 0),
                context_window=st.get("context_window", 0), model=st.get("model", ""),
                thinking=st.get("thinking"), activity=self._activity_label(),
            )
            try:
                from prompt_toolkit.application import get_app
                width = get_app().output.get_size().columns
            except Exception:
                width = None
            return ANSI("\n".join(render_footer(fs, width)))
        except Exception:
            return ANSI("")

    def _activity_label(self) -> str | None:
        """Short live-status label for the inline footer."""
        running_tools = [
            str(t.name) for t in self.state.active_tools.values()
            if getattr(t, "status", "") == "running" and getattr(t, "name", "")
        ]
        if running_tools:
            names = []
            for name in running_tools:
                if name not in names:
                    names.append(name)
            shown = ", ".join(names[:2])
            if len(names) > 2:
                shown += f" +{len(names) - 2}"
            return f"Running {shown}"
        if self._is_running() or self.state.mode == "running":
            return "Thinking..."
        return None

    def _modal_fragments(self):
        m = self.state.modal
        if m is None:
            return ANSI("")
        body = m.message + (f"\n  {m.command}" if m.command else "")
        return ANSI(f"{body}\n\n  y allow once   n deny")

    def _plan_fragments(self):
        m = self.state.plan_modal
        if m is None:
            return ANSI("")
        return ANSI(
            f"{m.plan_content}\n\n"
            "  1 clear context and execute   2 execute, keep context\n"
            "  3 execute, manually approve   4 keep planning"
        )

    # ── 选择器 overlay 渲染（Pi 单列带边框面板：header/search 在上、list 在下、无 preview）──────
    @staticmethod
    def _as_str(x) -> str:
        return x.value if isinstance(x, ANSI) else ("" if x is None else str(x))

    def _sel_size(self):
        try:
            from prompt_toolkit.application import get_app
            sz = get_app().output.get_size()
            return sz.columns, sz.rows
        except Exception:
            return 80, 30

    def _sel_panel_fragments(self):
        """整块单列面板：accent 上下边框 + header_lines + search_line + 空行 + 滚动窗 list + (i/total)。"""
        s = self.state.selector
        if s is None:
            return ANSI("")
        width, height = self._sel_size()
        accent, dim, reset = "\x1b[36m", "\x1b[2m", "\x1b[0m"
        lines: list[str] = [f"{accent}{'─' * width}{reset}"]
        for hl in s.model.header_lines(width):
            lines.append(self._as_str(hl))
        sl = s.model.search_line(width)
        if sl is not None:
            lines.append(self._as_str(sl))
        lines.append("")
        items = s.model.items()
        if not items:
            lines.append(f"{dim}  (no entries){reset}")
        else:
            n = len(items)
            idx = max(0, min(s.index, n - 1))
            mv = max(3, s.model.max_visible(height))
            start = max(0, min(idx - mv // 2, max(0, n - mv)))
            end = min(start + mv, n)
            for i in range(start, end):
                lines.append(self._as_str(s.model.list_text(items[i], i == idx, width)))
            lines.append(f"{dim}  ({idx + 1}/{n}){s.model.status_suffix()}{reset}")
        lines.append(f"{accent}{'─' * width}{reset}")
        return ANSI("\n".join(lines))

    def _ask_fragments(self):
        return ANSI(self.state.text_prompt or "")

    def _build_app(self, *, input=None, output=None) -> Application:
        kb = KeyBindings()
        has_modal = Condition(lambda: self.state.modal is not None)
        has_plan = Condition(lambda: self.state.plan_modal is not None)
        has_selector = Condition(lambda: self.state.selector is not None)
        has_ask = Condition(lambda: self.state.text_prompt is not None)
        # 普通输入键（提交/换行/补全）只在无任何 overlay 时活跃。
        plain = Condition(lambda: self.state.modal is None and self.state.plan_modal is None
                          and self.state.selector is None and self.state.text_prompt is None)

        @kb.add("enter", filter=plain)
        def _submit_key(event):
            text = self.input_buffer.text
            if text.strip():
                self.input_buffer.append_to_history()   # 记入历史（↑ 回溯）——自定义 enter 不走默认 accept
            self.input_buffer.reset()
            self._cancel_count = 0
            self._submit(text)

        @kb.add("up", filter=plain)
        def _hist_up(event):
            # 多行 buffer：在首行按 ↑ 回溯历史，否则上移光标（shell 式）。
            buf = self.input_buffer
            if buf.document.cursor_position_row == 0:
                buf.history_backward()
            else:
                buf.cursor_up()

        @kb.add("down", filter=plain)
        def _hist_down(event):
            buf = self.input_buffer
            if buf.document.cursor_position_row == buf.document.line_count - 1:
                buf.history_forward()
            else:
                buf.cursor_down()

        @kb.add("enter", filter=has_ask)
        def _ask_submit(event):
            fut = self._ask_future
            text = self.input_buffer.text
            if fut is not None and not fut.done():
                fut.set_result(text)

        @kb.add("c-j", filter=plain)        # Ctrl-J 换行（多行输入）
        @kb.add("escape", "enter", filter=plain)  # Meta-Enter 换行
        def _newline(event):
            self.input_buffer.insert_text("\n")

        @kb.add("y", filter=has_modal)
        def _approve(event):
            self._resolve_approval(True)

        @kb.add("n", filter=has_modal)
        @kb.add("escape", filter=has_modal)
        def _deny(event):
            self._resolve_approval(False)

        for _digit in ("1", "2", "3", "4"):
            @kb.add(_digit, filter=has_plan)
            def _plan_choice(event, _d=_digit):
                self._resolve_plan(_d)

        # ── 选择器导航/退出/extra 键（仅 has_selector 活跃）──────────────────────
        # 搜索态下（model.supports_query()）j/k/q 是**文本**而非导航/取消——只有方向键导航、
        # 只有 Esc 取消（对齐 Pi：搜索框里任何可打印字符都进 query）。
        sel_nav = Condition(
            lambda: self.state.selector is not None and not self.state.selector.model.supports_query()
        )

        @kb.add("up", filter=has_selector)
        def _sel_up(event):
            self._sel_move(-1)

        @kb.add("k", filter=sel_nav)
        def _sel_up_k(event):
            self._sel_move(-1)

        @kb.add("down", filter=has_selector)
        def _sel_down(event):
            self._sel_move(1)

        @kb.add("j", filter=sel_nav)
        def _sel_down_j(event):
            self._sel_move(1)

        @kb.add("enter", filter=has_selector)
        def _sel_enter(event):
            from .selector import Outcome
            s = self.state.selector
            if s is not None and s.model.confirming():
                self._sel_dispatch("confirm")   # destructive confirm (e.g. delete)
                return
            if self._sel_current() is not None:
                self._resolve_selector(Outcome("done", item=self._sel_current()))

        @kb.add("q", filter=sel_nav)
        def _sel_cancel_q(event):
            from .selector import Outcome
            self._resolve_selector(Outcome("cancel"))

        @kb.add("escape", filter=has_selector)
        def _sel_cancel_esc(event):
            from .selector import Outcome
            s = self.state.selector
            if s is not None and s.model.confirming():
                self._sel_dispatch("abort")     # back out of confirm, stay in selector
                return
            self._resolve_selector(Outcome("cancel"))

        @kb.add("pageup", filter=has_selector)
        def _sel_pageup(event):
            self._sel_move(-self._sel_page())

        @kb.add("pagedown", filter=has_selector)
        def _sel_pagedown(event):
            self._sel_move(self._sel_page())

        # owner extra_keys 中的**特殊/ctrl 键**（可打印字符走 Keys.Any → _sel_key，见下）。
        for _xk in ("tab", "c-r", "c-s", "c-n", "c-p", "c-d", "c-t", "c-u", "c-l", "c-a",
                    "c-o", "c-left", "c-right"):
            @kb.add(_xk, filter=has_selector)
            def _sel_extra(event, _k=_xk):
                self._sel_key(_k)

        @kb.add("backspace", filter=has_selector)
        @kb.add("c-h", filter=has_selector)
        def _sel_backspace(event):
            self._sel_query_backspace()

        @kb.add(Keys.Any, filter=has_selector)
        def _sel_any(event):
            data = event.data or ""
            if data and data.isprintable():
                # 经 _sel_key 统一路由：在 model.extra_keys 里 → on_key；否则 → query（如支持）。
                self._sel_key(data)

        @kb.add("escape", filter=has_ask)
        def _ask_cancel(event):
            fut = self._ask_future
            if fut is not None and not fut.done():
                fut.set_result(None)

        @kb.add("c-c")
        def _ctrl_c(event):
            if self.state.modal is not None:
                self._resolve_approval(False)
                return
            if self.state.plan_modal is not None:
                self._resolve_plan("4")   # keep-planning（不执行）
                return
            if self.state.selector is not None:
                from .selector import Outcome
                self._resolve_selector(Outcome("cancel"))
                return
            if self.state.text_prompt is not None:
                fut = self._ask_future
                if fut is not None and not fut.done():
                    fut.set_result(None)
                return
            if self._is_running():
                self._cancel_count = 0
                try:
                    self.thread.abort()
                except Exception:
                    pass
                return
            # idle：有输入先清行；空行连按两次退出
            if self.input_buffer.text:
                self.input_buffer.reset()
                self._cancel_count = 0
                return
            self._cancel_count += 1
            if self._cancel_count >= 2:
                event.app.exit()

        @kb.add("c-d", filter=plain)   # 仅在基础 prompt 生效——overlay 下 c-d 归 selector（如 resume 删除）
        def _ctrl_d(event):
            if not self.input_buffer.text:
                event.app.exit()

        footer = Window(FormattedTextControl(self._footer_fragments), height=2, style="class:footer")
        self._input_window = Window(
            BufferControl(buffer=self.input_buffer),
            height=Dimension(min=1, max=8),
            wrap_lines=True,
            get_line_prefix=lambda lineno, wrap_count: [("class:prompt", "> " if lineno == 0 and not wrap_count else "  ")],
        )
        modal = ConditionalContainer(
            Frame(Window(FormattedTextControl(self._modal_fragments), height=Dimension(min=3)), title="Approval"),
            filter=has_modal,
        )
        plan_modal = ConditionalContainer(
            Frame(Window(FormattedTextControl(self._plan_fragments), height=Dimension(min=4)), title="Plan Approval"),
            filter=has_plan,
        )
        # Pi 单列带边框面板（无 preview）：整块由 _sel_panel_fragments 渲一个 ANSI；focusable 吞导航键。
        self._sel_window = Window(FormattedTextControl(self._sel_panel_fragments, focusable=True),
                                  wrap_lines=False)
        selector_region = ConditionalContainer(
            HSplit([self._sel_window], height=Dimension(min=6, weight=1)),
            filter=has_selector,
        )
        ask_line = ConditionalContainer(
            Window(FormattedTextControl(self._ask_fragments), height=1, style="class:prompt"),
            filter=has_ask,
        )
        # selector 打开时面板独占（其 `Search:` 行即输入，位于列表上方）——隐藏 footer + 主输入框，
        # 否则主 `>` 会落在列表/详情下方（Pi：选择器接管视图，editor 不参与选择）。
        no_sel = ~has_selector
        footer_c = ConditionalContainer(footer, filter=no_sel)
        input_c = ConditionalContainer(self._input_window, filter=no_sel)
        root = FloatContainer(
            content=HSplit([selector_region, footer_c, ask_line, input_c]),
            floats=[
                Float(content=modal, top=1, left=2, right=2),
                Float(content=plan_modal, top=1, left=2, right=2),
                Float(xcursor=True, ycursor=True,
                      content=CompletionsMenu(max_height=12, scroll_offset=1)),  # 命令补全菜单
            ],
        )
        return Application(
            layout=Layout(root, focused_element=self._input_window),
            key_bindings=kb,
            full_screen=False,
            mouse_support=False,
            input=input,
            output=output,
        )

    # ── 运行 ──────────────────────────────────────────────────────────────────
    async def run(self, *, patch: bool = True) -> None:
        # 不用 patch_stdout：所有 transcript / 命令输出经 _emit_above→run_in_terminal 印到 app 之上
        # （patch_stdout 的被动 proxy 与 app 连续重绘相互覆盖，且会吞掉 run_in_terminal 内的 print）。
        self._loop = asyncio.get_running_loop()
        await self._app.run_async()
