"""Pi-style two-line footer renderer."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

from prompt_toolkit.utils import get_cwidth


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
