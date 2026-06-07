"""Terminal UI rendering — colored output, spinner, tool display."""

from __future__ import annotations

import re
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
CONNECTOR = "⎿"
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


def print_user_prompt() -> None:
    console.print("\n[bold green]>[/bold green] ", end="")


def print_assistant_text(text: str) -> None:  # kept for compatibility (legacy raw path)
    sys.stdout.write(text)
    sys.stdout.flush()


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


def print_tool_call(name, inp) -> None:
    title = _TOOL_TITLES.get(name, name)
    summary = _get_tool_summary(name, inp)
    line = f"[cyan]{BULLET}[/cyan] [bold]{escape(title)}[/bold]"
    if summary:
        line += f"([dim]{escape(summary)}[/dim])"
    console.print(line)


def print_tool_result(name, result) -> None:
    if name in ("edit_file", "write_file") and not result.startswith("Error"):
        _print_file_change_result(name, result)
        return
    console.print(f"  [dim]{CONNECTOR} {escape(_summarize_result(name, result))}[/dim]")


def _summarize_result(name, result) -> str:
    r = result or ""
    first = r.split("\n", 1)[0].strip()
    if r.startswith(("Error", "Command failed", "Command timed out")):
        return first or "error"
    if name == "read_file":
        return f"Read {(r.count(chr(10)) + 1) if r else 0} lines"
    if name == "grep_search":
        if r.startswith("No matches"):
            return "No matches"
        n = len([l for l in r.split("\n") if l and not l.startswith("... and ")])
        return f"{n} matches"
    if name == "list_files":
        if r.startswith("No files"):
            return "No files"
        n = len([l for l in r.split("\n") if l and not l.startswith("... and ")])
        return f"{n} files"
    if name in ("run_shell", "sandbox_shell"):
        if r.strip() == "(no output)":
            return "(no output)"
        for l in r.split("\n"):
            if l.strip():
                return l.strip()[:80]
        return "(no output)"
    return first[:80] or "done"


def _print_file_change_result(name, result) -> None:
    lines = result.split("\n")
    body = lines[1:]
    adds = sum(1 for l in body if l.startswith("+ "))
    dels = sum(1 for l in body if l.startswith("- "))
    if name == "write_file" and adds == 0 and dels == 0:
        m = re.search(r"\((\d+) lines?\)", lines[0])
        adds = int(m.group(1)) if m else 0
    console.print(f"  [dim]{CONNECTOR} +{adds} -{dels}[/dim]")
    nonempty = [l for l in body if l.strip()]
    shown = 0
    for line in nonempty:
        if shown >= 12:
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


def print_divider() -> None:
    return  # no-op; turn separation now comes from prompt spacing


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


# ─── Plan approval display ──────────────────────────────────


def print_plan_for_approval(plan_content) -> None:
    console.print(f"\n[cyan]{BULLET} Plan for approval[/cyan]")
    console.print(Markdown(plan_content))


def print_plan_approval_options() -> None:
    console.print("[yellow]Choose an option:[/yellow]")
    console.print("  [white]1[/white][dim] — clear context and execute (auto-accept edits)[/dim]")
    console.print("  [white]2[/white][dim] — execute, keep context (auto-accept edits)[/dim]")
    console.print("  [white]3[/white][dim] — execute, manually approve each edit[/dim]")
    console.print("  [white]4[/white][dim] — keep planning (give feedback)[/dim]")


# ─── Sub-agent display ──────────────────────────────────────


def print_sub_agent_start(agent_type, description) -> None:
    console.print(f"[magenta]{BULLET} {escape(f'Task[{agent_type}]')}[/magenta][dim] {escape(description)}[/dim]")


def print_sub_agent_end(agent_type, _description) -> None:
    console.print(f"  [dim]{CONNECTOR} {escape(agent_type)} done[/dim]")


# ─── Tool titles and summaries ──────────────────────────────

_TOOL_TITLES = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Update",
    "list_files": "List",
    "grep_search": "Grep",
    "run_shell": "Bash",
    "sandbox_shell": "Sandbox",
    "skill": "Skill",
    "agent": "Task",
}


def _get_tool_summary(name: str, inp: dict) -> str:
    if name == "read_file":
        return inp.get("file_path", "")
    if name == "write_file":
        return inp.get("file_path", "")
    if name == "edit_file":
        return inp.get("file_path", "")
    if name == "list_files":
        return inp.get("pattern", "")
    if name == "grep_search":
        return f'"{inp.get("pattern", "")}" in {inp.get("path", ".")}'
    if name == "run_shell":
        cmd = inp.get("command", "")
        return cmd[:60] + "..." if len(cmd) > 60 else cmd
    if name == "sandbox_shell":
        cmd = inp.get("command", "")
        image = inp.get("image", "python:3.12")
        summary = cmd[:60] + "..." if len(cmd) > 60 else cmd
        return f"[{image}] {summary}"
    if name == "skill":
        return inp.get("skill_name", "")
    if name == "agent":
        return f'[{inp.get("type", "general")}] {inp.get("description", "")}'
    return ""
