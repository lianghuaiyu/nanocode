"""Pi-style two-line footer renderer."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

from rich.cells import cell_len as get_cwidth   # 显示宽度（CJK 双宽）；取代 prompt_toolkit.get_cwidth


def format_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    if count < 1_000_000:
        return f"{round(count / 1000)}k"
    if count < 10_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1_000_000)}M"


def format_cwd(cwd: str, home: str | None) -> str:
    if not home:
        return cwd
    rc = os.path.realpath(cwd)
    rh = os.path.realpath(home)
    if rc == rh:
        return "~"
    prefix = rh + os.sep
    if rc.startswith(prefix):
        return "~" + os.sep + rc[len(prefix):]
    return cwd


_BRANCH_TTL = 3.0
_branch_cache: dict[str, tuple[float, str | None]] = {}


def git_branch(cwd: str) -> str | None:
    now = time.monotonic()
    hit = _branch_cache.get(cwd)
    if hit is not None and now - hit[0] < _BRANCH_TTL:
        return hit[1]
    branch: str | None = None
    try:
        r = subprocess.run(
            ["git", "--no-optional-locks", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0:
            branch = r.stdout.strip() or None
    except Exception:
        branch = None
    _branch_cache[cwd] = (now, branch)
    return branch


@dataclass
class FooterState:
    cwd: str
    home: str | None
    branch: str | None
    session_name: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    context_used: int
    context_window: int
    model: str
    thinking: str | None
    activity: str | None = None


def _justify(left: str, right: str, width: int) -> str:
    lw = get_cwidth(left)
    rw = get_cwidth(right)
    if lw + 2 + rw <= width:
        return left + " " * (width - lw - rw) + right
    avail = width - lw - 2
    if avail <= 0:
        return left
    out, acc = [], 0
    for ch in right:
        cw = get_cwidth(ch)
        if acc + cw > avail:
            break
        out.append(ch)
        acc += cw
    truncated = "".join(out)
    return left + " " * max(0, width - lw - acc) + truncated


def render_footer(state: FooterState, width: int | None = None) -> list[str]:
    pwd = format_cwd(state.cwd, state.home)
    if state.branch:
        pwd = f"{pwd} ({state.branch})"
    if state.session_name:
        pwd = f"{pwd} • {state.session_name}"

    parts: list[str] = []
    if state.input_tokens:
        parts.append(f"↑{format_tokens(state.input_tokens)}")
    if state.output_tokens:
        parts.append(f"↓{format_tokens(state.output_tokens)}")
    if state.cost_usd:
        parts.append(f"${state.cost_usd:.3f}")
    if state.context_window:
        pct = state.context_used / state.context_window * 100
        parts.append(f"{pct:.1f}%/{format_tokens(state.context_window)}")
    left = "  ".join(parts)

    right_parts = [state.model] if state.model else []
    if state.thinking:
        right_parts.append(state.thinking)
    if state.activity:
        right_parts.append(state.activity)
    right = " • ".join(right_parts)

    if width and width > 0:
        stats = _justify(left, right, width) if left else right
    else:
        stats = f"{left}    {right}" if left else right
    return [pwd, stats]


def key_hint(key: str, desc: str) -> str:
    """ANSI 提示片段:键名走 dim、描述走 muted(对位 Pi keybinding-hints.keyHint)。"""
    from . import theme as T
    return f"{T.DIM}{key}{T.RESET}{T.fg('muted')} {desc}{T.RESET}"


def hint_sep() -> str:
    """提示片段分隔符(muted ' · ')。"""
    from . import theme as T
    return f"{T.fg('muted')} · {T.RESET}"


def _truncate_cells(text: str, width: int) -> str:
    """按显示宽度裁剪 + 省略号(footer 行裁剪)。"""
    if width <= 0:
        return ""
    if get_cwidth(text) <= width:
        return text
    out, used, limit = [], 0, width - 1
    for ch in text:
        w = get_cwidth(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def render_footer_styled(state: "FooterState", width: int | None = None) -> list:
    """Pi 式两行 footer 的**着色**版(供 Rich Live viewport):全 dim,ctx% 按阈值着色
    (>90% error / >70% warning / else dim);model 右对齐。返回 list[rich.text.Text]。"""
    from rich.text import Text

    pwd = format_cwd(state.cwd, state.home)
    if state.branch:
        pwd = f"{pwd} ({state.branch})"
    if state.session_name:
        pwd = f"{pwd} • {state.session_name}"

    left_parts: list[str] = []
    if state.input_tokens:
        left_parts.append(f"↑{format_tokens(state.input_tokens)}")
    if state.output_tokens:
        left_parts.append(f"↓{format_tokens(state.output_tokens)}")
    if state.cost_usd:
        left_parts.append(f"${state.cost_usd:.3f}")
    left_str = "  ".join(left_parts)

    pct_str, pct_style = "", "dim"
    if state.context_window:
        pct = state.context_used / state.context_window * 100
        pct_str = f"{pct:.0f}%/{format_tokens(state.context_window)}"
        pct_style = "error" if pct > 90 else ("warning" if pct > 70 else "dim")

    right_parts = [state.model] if state.model else []
    if state.thinking:
        right_parts.append(state.thinking)
    if state.activity:
        right_parts.append(state.activity)
    right = " • ".join(right_parts)

    left_full_plain = "  ".join(s for s in (left_str, pct_str) if s)

    if width and width > 0:
        line1 = Text(_truncate_cells(pwd, width), style="dim")
    else:
        line1 = Text(pwd, style="dim")

    line2 = Text()
    if left_str:
        line2.append(left_str, style="dim")
    if pct_str:
        if left_str:
            line2.append("  ", style="dim")
        line2.append(pct_str, style=pct_style)

    if width and width > 0:
        used = get_cwidth(left_full_plain) + get_cwidth(right)
        if used + 2 > width:
            avail = max(0, width - get_cwidth(left_full_plain) - 2)
            right = _truncate_cells(right, avail)
            spacing = 2 if avail > 0 else max(1, width - get_cwidth(left_full_plain))
        else:
            spacing = max(2, width - used)
    else:
        spacing = 4
    line2.append(" " * spacing)
    if right:
        line2.append(right, style="dim")
    return [line1, line2]
