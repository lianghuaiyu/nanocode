"""tui/rich_app.py —— Rich Live 交互客户端（docs/18：Codex inline-viewport 模型）。

取代 prompt_toolkit `TuiApp`。一个 `Live(console=tui.console, screen=False)` 持有底部 viewport
（输入行 + footer + 正在流式变化的 active cell；modal；session 选择器面板），按事件显式刷新，
仅前台 turn 运行时低频 tick 驱动 thinking 动画。近期对话保留在 Live viewport 中以便像 Pi 一样重渲染可展开工具块；
显式 session/branch 导航才把持久 transcript replay 到 Live 上方的终端 scrollback。

嵌入式边界：本模块只 import `.{state,reducer,footer,selector,line_editor,primitives,theme,tooltext}`
与 rich；**零** import agent/session/tools。消费的是 `on_event(AgentEvent 信封)` + 注入的
`RuntimeThread`/`ApprovalManager` 公开面，与旧 `TuiApp` 同一契约（drop-in）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from markdown_it import MarkdownIt
from rich.cells import cell_len
from rich.console import Group
from rich.panel import Panel
from rich.padding import Padding
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich import box as _box

from .. import tui as _tui
from . import theme as _theme
from . import tooltext as _tt
from .line_editor import KeyParser, LineEditor, PasteToken, raw_mode, restore
from .reducer import hydrate_status, reduce
from .selector import Outcome
from .subagent_widget import MAX_WIDGET_LINES, TERMINAL_STATUSES, render_subagent_widget
from .state import (
    ApprovalModal,
    AssistantItem,
    ErrorItem,
    NoticeItem,
    PlanModal,
    SelectorState,
    SessionBoundaryItem,
    SubAgentItem,
    ThinkingItem,
    ToolItem,
    TuiState,
    UserItem,
)

_ACCENT = "\x1b[36m"
_RESET = "\x1b[0m"
_MARKDOWN = MarkdownIt("commonmark").enable("table")


def _ansi_lines(s: str) -> list[Text]:
    return [Text.from_ansi(line) for line in s.split("\n")]


def _as_str(x) -> str:
    return x.value if hasattr(x, "value") else ("" if x is None else str(x))


def _append_blank(lines: list) -> None:
    if lines and getattr(lines[-1], "plain", str(lines[-1])) == "":
        return
    lines.append(Text(""))


def _choice_options_text(labels: list[str], selected_index: int) -> Text:
    text = Text()
    for i, label in enumerate(labels):
        if i:
            text.append("   ")
        selected = i == selected_index
        text.append(("› " if selected else "  ") + label, style="bold cyan" if selected else "dim")
    return text


def _style(stack: list[str]) -> str:
    return " ".join(s for s in stack if s)


def _inline_text(token, *, plain: bool = False) -> str | Text:
    out = "" if plain else Text()
    stack: list[str] = []

    def append_text(value: str, style: str | None = None) -> None:
        nonlocal out
        if plain:
            out += value
        else:
            out.append(value, style=style or _style(stack) or None)

    def walk(children) -> None:
        for child in children or []:
            typ = child.type
            if typ == "text":
                append_text(child.content)
            elif typ in ("softbreak", "hardbreak"):
                append_text(" ")
            elif typ == "code_inline":
                append_text(child.content, "md_code")
            elif typ == "strong_open":
                stack.append("bold")
            elif typ == "strong_close":
                if stack:
                    stack.pop()
            elif typ == "em_open":
                stack.append("italic")
            elif typ == "em_close":
                if stack:
                    stack.pop()
            elif typ == "link_open":
                stack.append("md_link")
            elif typ == "link_close":
                if stack:
                    stack.pop()
            elif getattr(child, "children", None):
                walk(child.children)
            elif getattr(child, "content", ""):
                append_text(child.content)

    walk(getattr(token, "children", None) or [token])
    return out


def _truncate_cell(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if width == 1:
        return text[:1] if cell_len(text[:1]) <= 1 else ""
    if cell_len(text) <= width:
        return text
    out: list[str] = []
    used = 0
    limit = width - 1
    for ch in text:
        w = cell_len(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _pad_cell(text: str, width: int) -> str:
    return text + " " * max(0, width - cell_len(text))


def _parse_table(tokens, start: int) -> tuple[list[list[str]], int]:
    rows: list[list[str]] = []
    row: list[str] | None = None
    in_cell = False
    cell = ""
    i = start + 1
    while i < len(tokens):
        t = tokens[i]
        if t.type == "table_close":
            return rows, i
        if t.type == "tr_open":
            row = []
        elif t.type in ("th_open", "td_open"):
            in_cell = True
            cell = ""
        elif t.type == "inline" and in_cell:
            cell += str(_inline_text(t, plain=True))
        elif t.type in ("th_close", "td_close") and row is not None:
            row.append(cell.strip())
            in_cell = False
        elif t.type == "tr_close" and row is not None:
            rows.append(row)
            row = None
        i += 1
    return rows, i


def _render_table(rows: list[list[str]], width: int, raw_lines: list[str] | None = None) -> list[Text]:
    if not rows:
        return []
    cols = max(len(r) for r in rows)
    normalized = [r + [""] * (cols - len(r)) for r in rows]
    widths = [
        min(30, max(1, max(cell_len(r[c]) for r in normalized)))
        for c in range(cols)
    ]
    total = sum(widths) + (3 * (cols - 1))
    if total > width and raw_lines:
        return [Text(line) for line in raw_lines]
    out: list[Text] = []
    for ri, row in enumerate(normalized):
        parts = [_pad_cell(_truncate_cell(row[c], widths[c]), widths[c]) for c in range(cols)]
        out.append(Text(" │ ".join(parts), style="bold" if ri == 0 else None))
        if ri == 0 and len(normalized) > 1:
            out.append(Text("─┼─".join("─" * w for w in widths), style="muted"))
    return out


def _pi_markdown(text: str, width: int) -> Group:
    normalized = (text or "").replace("\t", " ")
    if not normalized.strip():
        return Group()
    source_lines = normalized.splitlines()
    tokens = _MARKDOWN.parse(normalized)
    lines: list = []
    list_stack: list[dict] = []
    current_list_prefix: str | None = None
    quote_depth = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        typ = token.type
        if typ == "heading_open":
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                level = int((token.tag or "h1")[1:] or "1")
                heading = _inline_text(tokens[i + 1])
                if isinstance(heading, Text):
                    heading.stylize("md_heading1" if level == 1 else "md_heading")
                    lines.append(heading)
            if i + 3 < len(tokens) and tokens[i + 3].type != "space":
                _append_blank(lines)
            i += 2
        elif typ == "paragraph_open":
            j = i + 1
            while j < len(tokens) and tokens[j].type != "paragraph_close":
                if tokens[j].type == "inline":
                    rendered = _inline_text(tokens[j])
                    if current_list_prefix:
                        # 仅给 marker 上色;item 文本保持自身样式(prefix 作为 span 而非整行 base style)
                        line = Text()
                        prefix = current_list_prefix
                        plain = rendered.plain
                        if prefix.endswith("• ") and plain[:4] in ("[ ] ", "[x] ", "[X] "):
                            checked = plain[1] in "xX"      # 任务清单:勾选框替代字面 [ ]/[x]
                            line.append(prefix[:-2], style="md_list_bullet")
                            line.append("☑ " if checked else "☐ ",
                                        style="success" if checked else "muted")
                            try:
                                rendered = rendered[4:]
                            except Exception:
                                rendered = Text(plain[4:])
                        else:
                            line.append(prefix, style="md_list_bullet")
                        line.append_text(rendered)
                        rendered = line
                    if quote_depth:
                        rendered.stylize("italic")
                        rendered = Text("│ " * quote_depth, style="md_quote") + rendered
                    lines.append(rendered)
                j += 1
            if not current_list_prefix and quote_depth == 0:
                _append_blank(lines)
            i = j
        elif typ == "bullet_list_open":
            list_stack.append({"ordered": False, "index": 0})
        elif typ == "ordered_list_open":
            start = int(token.attrGet("start") or 1)
            list_stack.append({"ordered": True, "index": start - 1})
        elif typ in ("bullet_list_close", "ordered_list_close"):
            if list_stack:
                list_stack.pop()
            if not list_stack:
                _append_blank(lines)
        elif typ == "list_item_open":
            if list_stack:
                list_stack[-1]["index"] += 1
                depth = max(0, len(list_stack) - 1)
                marker = f"{list_stack[-1]['index']}. " if list_stack[-1]["ordered"] else "• "
                current_list_prefix = "  " * depth + marker
        elif typ == "list_item_close":
            current_list_prefix = None
        elif typ == "blockquote_open":
            quote_depth += 1
        elif typ == "blockquote_close":
            quote_depth = max(0, quote_depth - 1)
            _append_blank(lines)
        elif typ == "fence":
            info = (token.info or "").strip().split(" ")[0] if (token.info or "").strip() else ""
            code = token.content.rstrip("\n")
            bg = _theme.hex_of("code_block_bg")
            syntax = None
            if info:                              # 已知语言 → Rich Syntax 高亮,底色用 code_block_bg
                try:
                    from rich.syntax import Syntax
                    from pygments.lexers import get_lexer_by_name
                    get_lexer_by_name(info)
                    syntax = Syntax(code, info, theme=_theme.CODE_SYNTAX_THEME, background_color=bg,
                                    word_wrap=False, padding=(0, 1))
                except Exception:
                    syntax = None
            _append_blank(lines)                  # 专用代码块:底色填充框 + 语言标签,不渲染字面 ``` 围栏
            if info:
                lines.append(Padding(Text(info, style="code_label"), (0, 1), style="code_label"))
            if syntax is not None:
                lines.append(syntax)
            else:
                lines.append(Padding(Text(code or " ", style="md_code_block"), (0, 1), style="code_block"))
            _append_blank(lines)
        elif typ == "code_block":
            for code_line in token.content.rstrip("\n").split("\n"):
                lines.append(Text(" " + code_line, style="md_code_block"))
            _append_blank(lines)
        elif typ == "hr":
            lines.append(Text("─" * min(max(1, width), 80), style="md_hr"))
            _append_blank(lines)
        elif typ == "table_open":
            table_rows, end = _parse_table(tokens, i)
            raw = source_lines[token.map[0]:token.map[1]] if token.map else None
            lines.extend(_render_table(table_rows, width, raw))
            _append_blank(lines)
            i = end
        elif typ == "inline":
            rendered = _inline_text(token)
            if quote_depth:
                rendered.stylize("italic")
                rendered = Text("│ " * quote_depth, style="md_quote") + rendered
            lines.append(rendered)
        i += 1
    while lines and getattr(lines[-1], "plain", str(lines[-1])) == "":
        lines.pop()
    return Group(*lines)


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

    _FOREGROUND_REFRESH_HZ = 4.0

    def __init__(self, *, input=None, output=None, registry=None, completer=None) -> None:
        self.state = TuiState()
        self.thread = None
        self._on_submit = None
        self._registry = registry            # 命令注册表(斜杠补全菜单的来源;CommandSpec.name/description/arg_hint)
        self._completer = completer           # 注入的文件搜索闭包(@-mention / Tab 路径补全),保持嵌入式边界
        self._console = output if output is not None else _tui.console
        try:                                  # 角色样式(user_message/tool_*/md_*…)挂到本 app 的 console
            _t = _theme.rich_theme()
            if _t is not None:
                self._console.push_theme(_t)
        except Exception:
            pass
        self._input = input                      # None=stdin；否则 fd / 有 fileno() 的对象（测试 pipe）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit: asyncio.Future | None = None
        self._unsubscribe = None
        self._turn_task: asyncio.Task | None = None
        self._approval_future: asyncio.Future | None = None
        self._plan_future: asyncio.Future | None = None
        self._selector_future: asyncio.Future | None = None
        self._ask_future: asyncio.Future | None = None
        self._selector_mouse_defer_disable = False
        self._transcript_scroll = 0
        self._parser = KeyParser()
        self._esc_timer = None
        self._editor = LineEditor(history=self._load_history())
        self.input_buffer = _InputProxy(self._editor)
        self._spinner = Spinner("dots", text=Text(" Working…", style="dim"), style="accent")
        self._tools_expanded = False
        self._turn_started: float | None = None
        self._live = None
        self._refresh_task: asyncio.Task | None = None
        self._subagent_widget_cache_at = 0.0
        self._subagent_widget_snapshot: list[dict] = []
        self._subagent_widget_frame = 0
        # ── autocomplete(斜杠命令 / @文件 / Tab 路径)状态 ──
        self._ac_items: list = []
        self._ac_index = 0
        self._ac_kind: str | None = None      # None=未激活;'command'|'mention'|'path'

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
        self.state.timeline.clear()
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
        if self._is_one_shot_notice(env):
            event = env.get("event")
            text = self._event_value(event, "text", "") or ""
            self._above_console().print(Text(text, style="accent"))
            self._refresh()
            return
        reduce(self.state, env)
        self._commit_finalized_event(env)
        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            try:
                self._live.refresh()
            except Exception:
                pass

    def _needs_periodic_refresh(self) -> bool:
        if (
            self.state.selector is not None
            or self.state.modal is not None
            or self.state.plan_modal is not None
            or self.state.text_prompt is not None
        ):
            return False
        return self._turn_task is not None and not self._turn_task.done()

    async def _refresh_loop(self) -> None:
        interval = 1.0 / max(1.0, float(self._FOREGROUND_REFRESH_HZ))
        while self._exit is not None and not self._exit.done():
            if self._needs_periodic_refresh():
                self._refresh()
            await asyncio.sleep(interval)

    def _above_console(self):
        live = self._live
        return getattr(live, "console", None) if live is not None else self._console

    def set_submit_handler(self, fn) -> None:
        self._on_submit = fn

    def print_above(self, text: str, *, error: bool = False) -> None:
        style = "red" if error else "cyan"
        self._above_console().print(Text(str(text), style=style if error else None))

    def refresh_transcript(self) -> None:
        """Replay the current branch transcript into terminal scrollback.

        This is used for explicit session/branch navigation (/resume, /tree). The live viewport
        stays small and stable; persisted history remains visible in terminal scrollback.
        """
        sid = self.state.status.session_id or getattr(self.thread, "session_id", "")
        self.state.timeline.clear()
        if sid:
            self.state.timeline.append(SessionBoundaryItem(session_id=sid))
        if self.thread is None:
            self._refresh()
            return
        try:
            snapshot = self.thread.state()
        except Exception:
            self._refresh()
            return
        messages = snapshot.get("transcript_messages") or []
        console = self._above_console()
        console.print(Text("Session context", style="dim"))
        if not messages:
            console.print(Text("  (empty)", style="dim"))
            console.print("")
            self._refresh()
            return
        for msg in messages:
            self._print_transcript_message(msg)
        console.print("")
        self._refresh()

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

    _PLAN_OPTIONS = (
        ("Clear + execute", {"choice": "clear-and-execute"}),
        ("Execute, keep plan", {"choice": "execute"}),
        ("Manual approve", {"choice": "manual-execute"}),
        ("Keep planning", {"choice": "keep-planning", "feedback": None}),
    )
    _PLAN_CHOICES = {
        "1": _PLAN_OPTIONS[0][1],
        "2": _PLAN_OPTIONS[1][1],
        "3": _PLAN_OPTIONS[2][1],
        "4": _PLAN_OPTIONS[3][1],
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
    async def run_selector(self, model, *, initial_index: int | None = None):
        loop = self._loop
        if loop is None:
            return Outcome("cancel", index=initial_index or 0)
        items = model.items()
        if initial_index is None:
            try:
                initial_index = int(model.initial_index())
            except Exception:
                initial_index = 0
        idx = 0 if not items else max(0, min(initial_index, len(items) - 1))
        self.state.selector = SelectorState(model=model, index=idx)
        self.state.mode = "selector"
        self._selector_mouse_defer_disable = False
        self._mouse_tracking(True)
        self._refresh()
        fut = loop.create_future()
        self._selector_future = fut
        try:
            return await fut
        finally:
            self._selector_future = None
            self.state.selector = None
            owner_turn = asyncio.current_task() is self._turn_task
            if owner_turn:
                self._selector_mouse_defer_disable = True
            else:
                self._mouse_tracking(False)
            still_in_owner_turn = owner_turn or self._is_running()
            self.state.mode = "running" if still_in_owner_turn else "idle"
            if not still_in_owner_turn:
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
        self._turn_started = time.monotonic()
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
        except Exception as e:
            msg = str(e) or type(e).__name__
            self.state.timeline.append(ErrorItem(text=msg))
            try:
                self.print_above(f"Error: {msg}", error=True)
            except Exception:
                pass
        finally:
            self._turn_started = None
            if self.state.mode == "running":
                self.state.mode = "idle"
            if self._selector_mouse_defer_disable:
                self._mouse_tracking(False)
                self._selector_mouse_defer_disable = False
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
        modal = self.state.modal
        if modal is None:
            return
        if tok in ("up", "left", "k"):
            modal.selected_index = max(0, modal.selected_index - 1)
        elif tok in ("down", "right", "j", "tab"):
            modal.selected_index = min(1, modal.selected_index + 1)
        elif tok == "enter":
            self._resolve(self._approval_future, modal.selected_index == 1)
        elif tok == "y":
            self._resolve(self._approval_future, True)
        elif tok in ("n", "escape", "ctrl-c"):
            self._resolve(self._approval_future, False)

    def _dispatch_plan(self, tok) -> None:
        modal = self.state.plan_modal
        if modal is None:
            return
        if tok in ("up", "left", "k"):
            modal.selected_index = max(0, modal.selected_index - 1)
        elif tok in ("down", "right", "j", "tab"):
            modal.selected_index = min(len(self._PLAN_OPTIONS) - 1, modal.selected_index + 1)
        elif tok == "enter":
            self._resolve(self._plan_future, dict(self._PLAN_OPTIONS[modal.selected_index][1]))
        elif tok in self._PLAN_CHOICES:
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
        if tok == "ctrl-o":
            self._toggle_tool_output_expansion()
            return
        if tok in ("scrollup", "pageup"):
            amount = 3 if tok == "scrollup" else max(1, self._transcript_height_budget() - 1)
            self._scroll_transcript(amount)
            return
        if tok in ("scrolldown", "pagedown"):
            amount = 3 if tok == "scrolldown" else max(1, self._transcript_height_budget() - 1)
            self._scroll_transcript(-amount)
            return
        # ── 补全菜单激活时优先吃导航/接受键 ──
        if self._ac_kind is not None and tok in ("up", "down", "tab", "enter", "escape"):
            if tok == "up":
                self._ac_move(-1)
            elif tok == "down":
                self._ac_move(1)
            elif tok == "escape":
                self._set_ac([], None)
            else:                                  # tab=填充;enter=填充(命令无参时直接提交)
                self._ac_accept(submit=(tok == "enter"))
            return
        if tok == "tab":                           # 菜单未激活:Tab 触发路径补全
            self._trigger_path_complete()
            return
        if tok == "escape" and self._is_running():  # 运行中 Esc → 优雅中断(与 footer 提示一致;Ctrl-C 同效)
            try:
                self.thread.abort()
            except Exception:
                pass
            return
        action = self._editor.handle(tok)
        if action == "submit":
            text = self._editor.text
            self._editor.reset()
            self._set_ac([], None)
            self._transcript_scroll = 0
            if text.strip():
                self._editor.add_history(text)
                self._save_history(text)
            self._submit(text)
        elif action == "cancel":              # Ctrl-C
            self._set_ac([], None)
            if self._is_running():
                try:
                    self.thread.abort()
                except Exception:
                    pass
            elif self._editor.text:
                self._editor.reset()
            else:
                self.request_exit()
        elif action == "eof":                 # Ctrl-D on empty
            self.request_exit()
        else:                                 # 文本变化后重算补全候选
            self._update_autocomplete()

    def _toggle_tool_output_expansion(self) -> None:
        self._set_tools_expanded(not self._tools_expanded)

    def _set_tools_expanded(self, expanded: bool) -> None:
        self._tools_expanded = expanded
        self._refresh()

    def _scroll_transcript(self, delta: int) -> None:
        self._transcript_scroll = max(0, self._transcript_scroll + delta)
        self._refresh()

    # ── autocomplete(斜杠命令 / @文件 / Tab 路径)───────────────────────
    @staticmethod
    def _token_before(before: str):
        """光标前的空白分隔 token:返回 (起始下标, token 串)。"""
        i = len(before)
        while i > 0 and not before[i - 1].isspace():
            i -= 1
        return i, before[i:]

    def _set_ac(self, items: list, kind: str | None) -> None:
        self._ac_items = items
        self._ac_kind = kind if items else None
        self._ac_index = 0

    def _ac_move(self, delta: int) -> None:
        if self._ac_items:
            self._ac_index = (self._ac_index + delta) % len(self._ac_items)

    def _update_autocomplete(self) -> None:
        text = self._editor.text
        cur = max(0, min(self._editor.cursor, len(text)))
        before = text[:cur]
        # 斜杠命令:首行以 / 开头、光标前无空格/换行
        if text.startswith("/") and "\n" not in before and " " not in before and self._registry is not None:
            q = before[1:].lower()
            items = []
            try:
                for spec in self._registry.specs():
                    name = spec.name.lstrip("/")
                    if q and q not in name.lower():
                        continue
                    hint = ((spec.arg_hint + "  ") if spec.arg_hint else "") + (spec.description or "")
                    items.append({
                        "insert": spec.name + (" " if spec.arg_hint else ""),
                        "label": spec.name, "hint": hint.strip(),
                        "submit": not spec.arg_hint, "replace": (0, cur),
                    })
            except Exception:
                items = []
            items.sort(key=lambda it: (not it["label"].lstrip("/").startswith(q), it["label"]))
            self._set_ac(items[:8], "command")
            return
        # @-mention:光标前 token 以 @ 起头
        start, token = self._token_before(before)
        if token.startswith("@") and self._completer is not None:
            try:
                paths = self._completer(token[1:], "mention")
            except Exception:
                paths = []
            items = [{"insert": "@" + p, "label": "@" + p, "hint": "", "submit": False,
                      "replace": (start, cur)} for p in paths[:8]]
            self._set_ac(items, "mention")
            return
        self._set_ac([], None)

    def _trigger_path_complete(self) -> None:
        if self._completer is None:
            return
        cur = self._editor.cursor
        start, token = self._token_before(self._editor.text[:cur])
        if not token:
            return
        try:
            paths = self._completer(token, "path")
        except Exception:
            paths = []
        if not paths:
            return
        items = [{"insert": p, "label": p, "hint": "", "submit": False,
                  "replace": (start, cur)} for p in paths[:8]]
        if len(items) == 1:                        # 唯一解:直接填充
            self._ac_items, self._ac_index, self._ac_kind = items, 0, "path"
            self._ac_accept(submit=False)
        else:
            self._set_ac(items, "path")

    def _submit_current(self) -> None:
        final = self._editor.text
        self._editor.reset()
        self._transcript_scroll = 0
        if final.strip():
            self._editor.add_history(final)
            self._save_history(final)
        self._submit(final)

    def _ac_accept(self, *, submit: bool) -> None:
        if not self._ac_items:
            self._set_ac([], None)
            return
        it = self._ac_items[self._ac_index]
        # Enter 且已键入的正是该候选全名(如 "/sandbox")→ 直接提交当前文本,让 handler 决定 bare 行为,
        # 不强制填充(否则带参命令 arg_hint 非空时 Enter 只填不跑,bare 命令永远无法用 Enter 执行)。
        if submit and it["insert"].strip() == self._editor.text.strip():
            self._set_ac([], None)
            self._submit_current()
            return
        start, end = it["replace"]
        text = self._editor.text
        prefix = text[:start] + it["insert"]
        self._editor.set_text(prefix + text[end:])
        self._editor.cursor = len(prefix)
        self._set_ac([], None)
        if submit and it.get("submit"):       # 无参命令(arg_hint 空):Enter 接受即执行
            self._submit_current()
        else:
            self._update_autocomplete()

    def _render_autocomplete(self):
        if self._ac_kind is None or not self._ac_items:
            return None
        rows: list = []
        for i, it in enumerate(self._ac_items):
            sel = i == self._ac_index
            line = Text("→ " if sel else "  ", style="accent" if sel else "dim")
            line.append(it["label"], style="ac_selected" if sel else "text")
            if it.get("hint"):
                line.append("  " + it["hint"], style="dim")
            rows.append(line)
        return Group(*rows)

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
            if s.model.wrap_navigation():
                s.index = (s.index + delta) % len(items)
            else:
                s.index = max(0, min(s.index + delta, len(items) - 1))

    def _sel_page(self, delta: int) -> None:
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
        key = self._selector_key(tok)
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
        elif tok == "scrollup":
            self._sel_move(-3)
        elif tok == "scrolldown":
            self._sel_move(3)
        elif tok == "pageup":
            self._sel_page(-max(1, m.max_visible(self._term_h())))
        elif tok == "pagedown":
            self._sel_page(max(1, m.max_visible(self._term_h())))
        elif tok == "left":
            self._sel_page(-max(1, m.max_visible(self._term_h())))
        elif tok == "right":
            self._sel_page(max(1, m.max_visible(self._term_h())))
        elif tok == "enter":
            if self._sel_current() is not None:
                self._resolve(self._selector_future, Outcome("done", item=self._sel_current(), index=s.index))
        elif tok == "escape" or (tok == "ctrl-c"):
            if tok == "escape" and query and m.escape_clears_query() and m.query():
                m.set_query("")
                self._sel_clamp()
                return
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
        elif key in m.extra_keys():
            self._apply_keyresult(m.on_key(key, self._sel_current(), s.index), key)
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
            if s is not None and isinstance(r.result, int):
                s.index = r.result
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
    def _selector_key(tok) -> str:
        if isinstance(tok, str) and tok.startswith("ctrl-") and len(tok) > len("ctrl-"):
            return "c-" + tok[len("ctrl-"):]
        return tok

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

    def _mouse_tracking(self, enabled: bool) -> None:
        fd = self._resolve_fd()
        if fd is None or not os.isatty(fd):
            return
        seq = "\x1b[?1000h\x1b[?1006h" if enabled else "\x1b[?1006l\x1b[?1000l"
        try:
            sys.stdout.write(seq)
            sys.stdout.flush()
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
            selected = max(0, min(m.selected_index, 1))
            return Panel(
                Group(
                    Text.from_ansi(body),
                    Text(""),
                    _choice_options_text(["Deny", "Allow once"], selected),
                    Text("  ↑↓ choose · Enter confirm · Esc deny", style="dim"),
                ),
                title="Approval",
                border_style="yellow",
            )
        if st.plan_modal is not None:
            selected = max(0, min(st.plan_modal.selected_index, len(self._PLAN_OPTIONS) - 1))
            return Panel(
                Group(
                    Text(st.plan_modal.plan_content),
                    Text(""),
                    _choice_options_text([label for label, _ in self._PLAN_OPTIONS], selected),
                    Text("  ↑↓ choose · Enter confirm · Esc keep planning", style="dim"),
                ),
                title="Plan Approval",
                border_style="cyan",
            )
        parts = []
        if st.text_prompt is not None:
            parts.append(Text(st.text_prompt, style="dim"))
            parts.append(self._editor.render(self._term_w(), prompt=""))
            return Group(*parts)
        # normal: 流式助手(⏺) + thinking spinner + 开口输入框 + footer（col2 左轨）
        transcript = self._render_timeline(max_lines=self._transcript_height_budget())
        if transcript is not None:
            parts.append(transcript)
        if st.mode == "running" and not self._has_open_assistant_text():
            self._spinner.update(text=Text(self._spinner_message(), style="dim"), style="accent")
            parts.append(self._spinner)
        widget = self._render_subagent_widget()
        if widget is not None:
            parts.append(Padding(widget, (0, 0, 0, 2)))
        parts.append(Text(""))               # 输入框前空一行（Codex 组间留空）
        parts.append(self._input_frame())
        ac = self._render_autocomplete()      # 斜杠/@文件/路径补全菜单(输入框下方,无边框缩进)
        if ac is not None:
            parts.append(Padding(ac, (0, 0, 0, 2)))
        foot = self._status_line()
        if foot is not None:
            parts.append(Padding(foot, (0, 0, 0, 2)))   # FOOTER_INDENT_COLS=2
        return Group(*parts)

    def _spinner_message(self) -> str:
        """阶段感知的 working 文案:运行中工具名 / Working,带经过秒数 + Esc 中断提示。"""
        phase = "Working"
        try:
            tools = list(self.state.active_tools.values())
            if tools:
                from . import tooltext as _t
                phase = f"Running {_t.tool_title(tools[-1].name)}"
        except Exception:
            pass
        elapsed = ""
        if self._turn_started is not None:
            secs = int(time.monotonic() - self._turn_started)
            if secs >= 1:
                elapsed = f" {secs}s"
        return f" {phase}…{elapsed}  (esc to interrupt)"

    # ── 开口框（HORIZONTALS：上下横线、左右开口；V1 设计）──────────────────────
    @staticmethod
    def _open_frame(body, *, title=None, subtitle=None, border="cyan"):
        return Panel(body, box=_box.HORIZONTALS, border_style=border, padding=0,
                     title=title, subtitle=subtitle, title_align="left", subtitle_align="right")

    def _input_frame(self):
        """底部输入开口框(裸上下横线,无标题/副标题;Pi editor 风格):正文 `> ` gutter;
        边框随 thinking 档位/bash 模式动态着色(model/ctx% 已移到 footer)。"""
        body = self._editor.render(self._term_w())   # "> text" + 光标块
        return self._open_frame(body, border=self._input_border_role())

    def _input_border_role(self) -> str:
        """`!` 前缀→bash_mode green;否则 thinking 档位 6 色;否则中性 border_muted。"""
        if self._editor.text.startswith("!"):
            return "bash_mode"
        level = None
        if self.thread is not None:
            try:
                level = self.thread.status().get("thinking")
            except Exception:
                level = None
        return _theme.thinking_border_role(level)

    def _status_line(self):
        """Pi 式两行 footer(col2 缩进):第1行 cwd (branch) · name;第2行 ↑in ↓out $cost ctx% │ model·thinking。
        ctx% 用当轮 context_used,阈值着色(>90 红 / >70 黄)。"""
        if self.thread is None:
            return None
        try:
            from .footer import FooterState, git_branch, render_footer_styled
            s = self.thread.status()
            cwd = s.get("cwd", "")
            state = FooterState(
                cwd=cwd, home=os.path.expanduser("~"), branch=git_branch(cwd),
                session_name=s.get("session_name"),
                input_tokens=s.get("input_tokens", 0) or 0,
                output_tokens=s.get("output_tokens", 0) or 0,
                cost_usd=s.get("cost_usd") or 0.0,
                context_used=s.get("context_used", 0) or 0,
                context_window=s.get("context_window", 0) or 0,
                model=s.get("model", "") or "",
                thinking=s.get("thinking"),
            )
            lines = list(render_footer_styled(state, max(10, self._term_w() - 2)))
            if s.get("is_subagent_session") and s.get("parent_session_id"):
                parent = str(s["parent_session_id"])
                lines.append(Text(f"sub-agent session · parent …{parent[-8:]} · /agent prev|next", style="dim"))
            return Group(*lines)
        except Exception:
            return None

    def _subagent_records_for_widget(self) -> list[dict]:
        if self.thread is None:
            return []
        now = time.monotonic()
        if now - self._subagent_widget_cache_at < 0.5:
            return self._subagent_widget_snapshot
        snapshot = self.thread.subagent_widget_snapshot()
        self._subagent_widget_snapshot = list(snapshot or [])
        self._subagent_widget_cache_at = now
        return self._subagent_widget_snapshot

    def _render_subagent_widget(self):
        records = self._subagent_records_for_widget()
        self._subagent_widget_frame += 1
        return render_subagent_widget(
            records,
            width=max(20, self._term_w() - 2),
            frame=self._subagent_widget_frame,
        )

    def _commit_finalized_event(self, env: dict) -> None:
        kind = env.get("type")
        event = env.get("event")
        if kind == "notice_raised":
            text = self._event_value(event, "text", "") or ""
            if text.startswith("Session → "):
                self._above_console().print(Text(text, style="accent"))
                self._remove_notice_text(text)
        elif kind in ("turn_completed", "turn_aborted", "error_raised"):
            self._commit_all_timeline()

    def _commit_all_timeline(self) -> None:
        """把 live timeline 全部条目(SessionBoundary 除外)按序写入 scrollback 并移出 timeline。

        连续工具块之间**不留空行**——否则各自的色块被无背景空行隔开,背景会「断层」;其余块后留空行。"""
        console = self._above_console()
        remaining: list = []
        rendered: list = []
        for item in self.state.timeline:
            if isinstance(item, SessionBoundaryItem):
                remaining.append(item)
                continue
            block = self._render_message_block(item)
            if block is not None:
                rendered.append((item, block))
        for idx, (item, block) in enumerate(rendered):
            console.print(block)
            nxt = rendered[idx + 1][0] if idx + 1 < len(rendered) else None
            if not (isinstance(item, ToolItem) and isinstance(nxt, ToolItem)):
                console.print("")
        self.state.timeline = remaining

    @staticmethod
    def _event_value(event, name: str, default=None):
        if isinstance(event, dict):
            return event.get(name, default)
        return getattr(event, name, default)

    def _is_one_shot_notice(self, env: dict) -> bool:
        if env.get("type") != "notice_raised":
            return False
        text = self._event_value(env.get("event"), "text", "") or ""
        return text.startswith("Session → ") or text.startswith("Background sub-agent run ")

    def _remove_notice_text(self, text: str) -> None:
        self.state.timeline = [
            item
            for item in self.state.timeline
            if not (isinstance(item, NoticeItem) and item.text == text)
        ]

    def _print_user_message(self, text: str) -> None:
        try:
            block = self._render_message_block(UserItem(text=text))
            if block is not None:
                console = self._above_console()
                console.print(block)
                console.print("")
        except Exception:
            pass

    def _print_transcript_message(self, msg: dict) -> None:
        role = msg.get("role")
        content = msg.get("content")
        console = self._above_console()
        if role == "user":
            self._print_user_message(self._message_text(content))
        elif role == "assistant":
            thinking = self._message_thinking(content)
            text = self._message_text(content)
            if thinking.strip():
                block = self._render_message_block(ThinkingItem(text=thinking))
                if block is not None:
                    console.print(block)
            if text.strip():
                block = self._render_message_block(AssistantItem(text=text))
                if block is not None:
                    console.print(block)
        elif role == "toolResult":
            text = self._message_text(content)
            first = text.split("\n", 1)[0].strip() if text else ""
            label = msg.get("toolName") or "tool result"
            if first:
                label += f": {first}"
            style = "error" if msg.get("isError") else "dim"
            console.print(Text(f"  ↳ {label}", style=style))

    @staticmethod
    def _message_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                typ = block.get("type")
                if typ == "text":
                    parts.append(block.get("text", ""))
                elif typ == "thinking":
                    continue
                elif typ == "toolUse":
                    parts.append(f"[tool call: {block.get('name', 'tool')}]")
                elif typ == "toolResult":
                    parts.append(block.get("content", ""))
            return "\n".join(p for p in parts if p)
        return "" if content is None else str(content)

    @staticmethod
    def _message_thinking(content) -> str:
        if isinstance(content, list):
            return "\n".join(
                block.get("thinking", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "thinking" and block.get("thinking")
            )
        return ""

    def _transcript_height_budget(self) -> int:
        reserve = 6
        if self.state.mode == "running" and not self._has_open_assistant_text():
            reserve += 1
        reserve += self._subagent_widget_height_estimate()
        return max(1, self._term_h() - reserve)

    def _subagent_widget_height_estimate(self) -> int:
        records = self._subagent_records_for_widget()
        if not records:
            return 0
        body = 0
        for rec in records:
            status = rec["status"]
            if status == "running":
                body += 2
            elif status == "queued" or status in TERMINAL_STATUSES:
                body += 1
        return min(MAX_WIDGET_LINES, 1 + body) if body else 0

    def _render_timeline(self, *, max_lines: int | None = None):
        items = [it for it in self.state.timeline if not isinstance(it, SessionBoundaryItem)]
        if not items:
            return None
        parts = []
        for item in items:
            rendered = self._render_timeline_item(item)
            if rendered is not None:
                parts.append(rendered)
        if not parts:
            return None
        group = Group(*parts)
        return self._viewport_renderable_lines(group, max_lines) if max_lines is not None else group

    def _viewport_renderable_lines(self, renderable, max_lines: int):
        if max_lines <= 0:
            return None
        try:
            lines = self._console.render_lines(
                renderable,
                self._console.options.update(width=self._term_w()),
                pad=False,
                new_lines=True,
            )
        except Exception:
            return renderable
        if len(lines) <= max_lines:
            self._transcript_scroll = 0
            return renderable

        # _transcript_scroll means "visual lines below the viewport". 0 follows the bottom.
        # Keep the viewport filled with transcript content only; scroll hints consume scarce
        # terminal rows and can push input/options off-screen.
        max_scroll = max(0, len(lines) - max_lines)
        scroll = max(0, min(self._transcript_scroll, max_scroll))
        self._transcript_scroll = scroll
        end = len(lines) - scroll
        start = max(0, end - max_lines)
        return Group(*(self._segments_to_text(line) for line in lines[start:end]))

    @staticmethod
    def _segments_to_text(line) -> Text:
        text = Text()
        for segment in line:
            if not segment.control:
                value = segment.text.replace("\n", "")
                if value:
                    text.append(value, style=segment.style)
        return text

    def _render_timeline_item(self, item):
        return self._render_message_block(item)

    def _render_message_block(self, item):
        """单一渲染真源——live 视口(_render_timeline_item)与 scrollback 提交(_commit_all_timeline)
        共用,保证 finalize 零跳变。"""
        if isinstance(item, UserItem):
            if not item.text.strip():
                return None
            return Padding(Text(item.text.strip(), style="user_message"), (1, 1), style="user_message")
        if isinstance(item, AssistantItem):
            if not item.text.strip():
                return None
            return self._assistant_grid("[cyan]⏺[/cyan]", self._markdown(item.text.strip()))
        if isinstance(item, ThinkingItem):
            if not item.text.strip():
                return None
            return self._assistant_grid("[thinking_text]◌[/]",
                                        Text(item.text.strip(), style="thinking"))
        if isinstance(item, ToolItem):
            return self._render_tool_box(item)
        if isinstance(item, NoticeItem):
            style = "warning" if item.level in ("warn", "retry") else "accent"
            return Text(f"  {item.text}", style=style)
        if isinstance(item, ErrorItem):
            return Text(f"  Error: {item.text}", style="error")
        if isinstance(item, SubAgentItem):
            return Text(f"  Task[{item.agent_type}] {item.description} ({item.status})", style="dim")
        return None

    def _render_tool_box(self, item):
        """状态着色的工具块(对位 Pi tool-execution):标题行 + 结果预览/diff,整块填充三态背景。"""
        if _tt.is_run_query_tool(item.name):
            line = _tt.run_query_line(
                item.name,
                item.input,
                item.result_excerpt or "",
                running=item.status == "running",
            )
            style = "error" if item.status == "error" else "tool_output"
            return Padding(Text(line, style=style), (0, 1))
        title = _tt.tool_title(item.name)
        summary = _tt.tool_summary(item.name, item.input)
        status = item.status
        fill = {"running": "tool_pending", "error": "tool_error",
                "denied": None, "done": "tool_success"}.get(status, "tool_success")
        head = Text()
        if item.name == "run_shell":
            head.append(f"$ {summary or '...'}", style="tool_title")
        else:
            head.append(title, style="tool_title")
            if summary:
                head.append(f" {summary}", style="tool_output")
        lines: list = [head]
        preview, extra_preview, total_preview = _tt.input_preview_lines(
            item.name, item.input, expanded=self._tools_expanded
        )
        for line in preview:
            lines.append(Text(line, style="tool_output"))
        if extra_preview > 0:
            lines.append(
                Text(
                    f"... ({extra_preview} more lines, {total_preview} total, Ctrl+O to expand)",
                    style="tool_output",
                )
            )
        result = item.result_excerpt or ""
        diff = _tt.parse_diff(item.name, result)
        if diff is not None:
            _adds, _dels, raw = diff
            shown = 0
            nonempty = [l for l in raw if l.strip()]
            max_lines = len(nonempty) if self._tools_expanded else 12
            for line in nonempty:
                if shown >= max_lines:
                    break
                if line.startswith("+ "):
                    lines.append(Text(line, style="diff_added"))
                elif line.startswith("- "):
                    lines.append(Text(line, style="diff_removed"))
                elif line.startswith("@@"):
                    lines.append(Text(line, style="accent"))
                else:
                    lines.append(Text(line, style="diff_context"))
                shown += 1
            if not self._tools_expanded and len(nonempty) > shown:
                lines.append(Text(f"… (+{len(nonempty) - shown} lines, Ctrl+O to expand)", style="tool_output"))
        elif status == "denied":
            if item.result_summary:
                lines.append(Text(item.result_summary, style="warning"))
        elif status != "running" and result and not _tt.suppress_success_result(item.name, result):
            if self._tools_expanded:
                out, _ = _tt.output_lines(result, None)
                for l in out:
                    lines.append(Text(l, style="tool_output"))
            else:
                limit = _tt.preview_limit(item.name, is_error=(status == "error"))
                tail = _tt.preview_from_tail(item.name)
                out, extra = _tt.output_lines(result, limit, tail=tail) if limit is not None else ([], 0)
                for l in out:
                    lines.append(Text(l, style="tool_output"))
                if extra > 0:
                    hidden = "earlier" if tail else "more"
                    lines.append(Text(f"… ({extra} {hidden} lines, Ctrl+O to expand)", style="tool_output"))
                elif not out and item.name not in ("read_file",):
                    lines.append(Text(_tt.result_summary(item.name, result), style="tool_output"))
        body = Group(*lines)
        if fill is None:                       # denied:warning 文,无背景填充
            return Padding(body, (0, 1))
        return Padding(body, (0, 1), style=fill)

    def _assistant_grid(self, marker, body):
        """marker(⏺/◌)在第 1 列、正文在第 2 列的网格——正文(含 Syntax/表格等块)**原生渲染**,
        marker 只贴首行、后续行自动缩进。取代逐 segment 重拼(对 Syntax 的整宽背景填充/折行会出错)。"""
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(overflow="fold")
        grid.add_row(Text.from_markup(marker), body)
        return grid

    def _markdown(self, text: str):
        return _pi_markdown(text, max(20, self._term_w() - 4))

    def _has_open_assistant_text(self) -> bool:
        for it in reversed(self.state.timeline):
            if isinstance(it, (AssistantItem, ThinkingItem)):
                if not it.complete and it.text:
                    return isinstance(it, AssistantItem)
                return False
        return False

    def _has_uncommitted_response_text(self) -> bool:
        return any(
            isinstance(it, (AssistantItem, ThinkingItem)) and bool(it.text)
            for it in self.state.timeline
        )

    def _render_selector(self):
        s = self.state.selector
        m = s.model
        w, h = self._term_w(), self._term_h()
        border = (_theme.fg("accent") if m.border_accent() else _theme.fg("border")) or _ACCENT
        rule = f"{border}{'─' * w}{_RESET}"
        lines: list = [Text("")]
        lines.append(Text.from_ansi(rule))
        lines.append(Text(""))
        for hl in m.header_lines(w):
            lines += _ansi_lines(_as_str(hl))
        lines.append(Text(""))
        sl = m.search_line(w)
        if sl is not None:
            lines += _ansi_lines(_as_str(sl))
        if m.body_border_after_search():
            lines.append(Text.from_ansi(rule))
        lines.append(Text(""))
        items = m.items()
        if not items:
            lines += _ansi_lines(f"{_theme.DIM}{m.empty_text(w)}{_RESET}")
            pos = m.position_line(0, 0, 0, 0, w)
            if pos is not None:
                lines += _ansi_lines(f"{_theme.DIM}{pos}{_RESET}")
        else:
            n = len(items)
            idx = max(0, min(s.index, n - 1))
            mv = max(3, m.max_visible(h))
            start = max(0, min(idx - mv // 2, max(0, n - mv)))
            end = min(start + mv, n)
            for i in range(start, end):
                lines += _ansi_lines(_as_str(m.list_text(items[i], i == idx, w)))
            pos = m.position_line(idx, n, start, end, w)
            if pos is not None:
                lines += _ansi_lines(f"{_theme.DIM}{pos}{_RESET}")
        lines.append(Text(""))
        lines.append(Text.from_ansi(rule))
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
            with Live(self, console=self._console, screen=False,
                      refresh_per_second=self._FOREGROUND_REFRESH_HZ,
                      auto_refresh=False, transient=False) as live:
                self._live = live
                self._refresh()
                self._refresh_task = asyncio.create_task(self._refresh_loop())
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
            if self._turn_task is not None and not self._turn_task.done():
                try:
                    if self.thread is not None:
                        self.thread.abort()
                except Exception:
                    pass
                self._turn_task.cancel()
                try:
                    await self._turn_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if self._refresh_task is not None and not self._refresh_task.done():
                self._refresh_task.cancel()
                try:
                    await self._refresh_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            self._refresh_task = None
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
