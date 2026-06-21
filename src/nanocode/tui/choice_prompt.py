"""Small synchronous terminal choice prompts used before RichApp exists."""

from __future__ import annotations

import os
import select
import sys
from pathlib import Path
from typing import Any, Sequence

from .line_editor import KeyParser, raw_mode, restore

Choice = tuple[str, Any] | tuple[str, Any, str]


def _choice_parts(choice: Choice) -> tuple[str, Any, str]:
    label = choice[0]
    value = choice[1]
    desc = choice[2] if len(choice) > 2 else ""
    return label, value, desc


def choose_terminal(
    title_lines: Sequence[str],
    choices: Sequence[Choice],
    *,
    default_index: int = 0,
    input_file=None,
    output_file=None,
) -> Any | None:
    """Render a tiny arrow-key selector and return the selected value.

    This is intentionally smaller than RichApp: it is used during startup trust
    gating, before project config and the long-lived TUI are allowed to load.
    """
    if not choices:
        return None
    input_file = input_file or sys.stdin
    output_file = output_file or sys.stdout
    try:
        fd = input_file.fileno()
    except Exception:
        return None
    if not os.isatty(fd) or not getattr(output_file, "isatty", lambda: False)():
        return None

    selected = max(0, min(default_index, len(choices) - 1))
    parser = KeyParser()
    row_count = len(choices) + 1

    def render(*, first: bool = False) -> None:
        if not first:
            output_file.write(f"\x1b[{row_count}A")
        for i, choice in enumerate(choices):
            label, _, desc = _choice_parts(choice)
            prefix = "›" if i == selected else " "
            suffix = f"  {desc}" if desc else ""
            output_file.write(f"\r\x1b[2K  {prefix} {label}{suffix}\n")
        output_file.write("\r\x1b[2K  ↑↓ navigate · Enter select · Esc cancel\n")
        output_file.flush()

    saved = None
    try:
        saved = raw_mode(fd)
        output_file.write("\x1b[?25l")
        for line in title_lines:
            output_file.write(str(line) + "\n")
        output_file.write("\n")
        render(first=True)
        while True:
            try:
                data = os.read(fd, 64)
            except OSError:
                return None
            if not data:
                return None
            tokens = parser.feed(data)
            if parser.pending_is_escape():
                ready, _, _ = select.select([fd], [], [], 0.04)
                if not ready:
                    tokens.extend(parser.flush_escape())
            for tok in tokens:
                if tok in ("up", "left", "k"):
                    selected = max(0, selected - 1)
                    render()
                elif tok in ("down", "right", "j", "tab"):
                    selected = min(len(choices) - 1, selected + 1)
                    render()
                elif tok == "enter":
                    return _choice_parts(choices[selected])[1]
                elif tok in ("escape", "ctrl-c", "ctrl-d", "q"):
                    return None
                elif isinstance(tok, str) and len(tok) == 1:
                    lowered = tok.lower()
                    for i, choice in enumerate(choices):
                        label, _, _ = _choice_parts(choice)
                        if label[:1].lower() == lowered:
                            selected = i
                            render()
                            break
    finally:
        if saved is not None:
            restore(fd, saved)
        try:
            output_file.write("\x1b[?25h")
            output_file.flush()
        except Exception:
            pass


def workspace_trust_choice(cwd: Path) -> bool | None:
    return choose_terminal(
        [
            "⚠ 工作区信任确认",
            f"  当前目录：{cwd}",
            "  这是你创建或信任的项目吗？nanocode 将加载此目录下的 .nanocode/ 配置",
            "  （权限规则、MCP server、技能、子 agent）并可读改/执行其中文件。",
            "  若非你的项目，请先退出审查内容。",
        ],
        [
            ("No, exit", False),
            ("Yes, trust and continue", True),
        ],
        default_index=0,
    )
