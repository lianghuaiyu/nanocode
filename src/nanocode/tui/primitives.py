"""tui —— 与 agent 无关的终端渲染框架（docs/17：Pi `packages/tui` 对位层）。

只提供**通用**原语：rich console、markdown / thinking 渲染、bullet / connector / diff 行、
spinner、plan/approval/confirmation 显示部件、颜色化 print。**不含任何领域知识**——工具名→标题、
结果摘要、文件改动解析等 agent 领域渲染在客户端侧 `entrypoints/render.py`。core 不 import 本模块
（渲染只在订阅端 client）。
"""

from __future__ import annotations

import sys
import threading
import time

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)

BULLET = "⏺"
CONNECTOR = "↳"
THINK = "✻"

# 详细程度：默认安静（不自动打印每轮 Tokens/cost、MCP 成功连接日志）。
# --verbose / NANOCODE_VERBOSE 打开。工具调用回显始终保留（coding agent 核心可观测性）。
_verbose = False


def set_verbose(value: bool) -> None:
    global _verbose
    _verbose = bool(value)


def is_verbose() -> bool:
    return _verbose


# ─── Basic output ──────────────────────────────────────────


def print_welcome() -> None:
    console.print("[bold cyan]nanocode[/bold cyan][dim] — a coding agent. Type a request, or 'exit' to quit.[/dim]")
    console.print("[dim]/clear /plan /cost /compact /memory /skills /sandbox /tasks /agents · !cmd runs shell · /<skill>[/dim]")


def render_assistant_markdown(text: str) -> None:
    # skip blank; render cyan ⏺ gutter in col 1 aligned with markdown body in col 2
    if not text.strip():
        return
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(f"[cyan]{BULLET}[/cyan]", Markdown(text.strip()))
    console.print(grid)


def render_thinking(text: str) -> None:
    if not text.strip():
        return
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(f"[dim]{THINK}[/dim]", Text(text.strip(), style="dim italic"))  # Text avoids markup injection
    console.print(grid)


def print_bullet(title: str, summary: "str | None" = None) -> None:
    """通用 ⏺ 行：cyan bullet + bold title + 可选 dim (summary)。领域调用方（render.py）传好文案。"""
    line = f"[cyan]{BULLET}[/cyan] [bold]{escape(title)}[/bold]"
    if summary:
        line += f"([dim]{escape(summary)}[/dim])"
    console.print(line)


def print_connector(text: str) -> None:
    """通用 ↳ 摘要行（dim）。"""
    console.print(f"  [dim]{CONNECTOR} {escape(text)}[/dim]")


def print_diff(adds: int, dels: int, body_lines: list, max_lines: int = 12) -> None:
    """通用 diff 块渲染：+adds -dels 头 + 着色的 @@/+/- 行（领域方负责解析出 body_lines）。"""
    console.print(f"  [dim]{CONNECTOR} +{adds} -{dels}[/dim]")
    nonempty = [l for l in body_lines if l.strip()]
    shown = 0
    for line in nonempty:
        if shown >= max_lines:
            break
        if line.startswith("@@"):
            console.print(f"     [cyan]{escape(line)}[/cyan]")
        elif line.startswith("- "):
            console.print(f"     [red]{escape(line)}[/red]")
        elif line.startswith("+ "):
            console.print(f"     [green]{escape(line)}[/green]")
        else:
            console.print(f"     [dim]{escape(line)}[/dim]")
        shown += 1
    if len(nonempty) > shown:
        console.print(f"     [dim]... ({len(nonempty) - shown} more lines)[/dim]")


def print_error(msg) -> None:
    console.print(f"[red]{BULLET} Error:[/red] {escape(str(msg))}")


def print_confirmation(command) -> None:
    console.print(f"[yellow]⚠ Dangerous command:[/yellow] {escape(command)}")


def print_retry(attempt, max_retries, reason) -> None:
    console.print(f"[yellow]↻ retry {attempt}/{max_retries}: {escape(str(reason))}[/yellow]")


def print_info(msg) -> None:
    console.print(f"[cyan]ℹ {escape(str(msg))}[/cyan]")


def print_cost(input_tokens, output_tokens) -> None:
    if not _verbose:
        return
    total = (input_tokens / 1_000_000) * 3 + (output_tokens / 1_000_000) * 15
    console.print(f"[dim]  Tokens {input_tokens} in / {output_tokens} out · ~${total:.4f}[/dim]")


# ─── Spinner ──────────────────────────────────────────────

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_spinner_thread: threading.Thread | None = None
_spinner_stop = threading.Event()


def start_spinner(label: str = "Thinking") -> None:
    global _spinner_thread
    if _spinner_thread is not None:
        return
    _spinner_stop.clear()

    def _run() -> None:
        frame = 0
        sys.stdout.write(f"\n  {SPINNER_FRAMES[0]} {label}...")
        sys.stdout.flush()
        while not _spinner_stop.is_set():
            time.sleep(0.08)
            frame = (frame + 1) % len(SPINNER_FRAMES)
            sys.stdout.write(f"\r  {SPINNER_FRAMES[frame]} {label}...")
            sys.stdout.flush()

    _spinner_thread = threading.Thread(target=_run, daemon=True)
    _spinner_thread.start()


def stop_spinner() -> None:
    global _spinner_thread
    if _spinner_thread is None:
        return
    _spinner_stop.set()
    _spinner_thread.join(timeout=1)
    _spinner_thread = None
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


# ─── Sub-agent display ──────────────────────────────────────


def print_sub_agent_start(agent_type, description) -> None:
    console.print(f"[magenta]{BULLET} {escape(f'Task[{agent_type}]')}[/magenta][dim] {escape(description)}[/dim]")


def print_sub_agent_end(agent_type, _description) -> None:
    console.print(f"  [dim]{CONNECTOR} {escape(agent_type)} done[/dim]")
