"""entrypoints/interactive/footer.py — Pi 式两行状态栏页脚（移植自 pi footer.ts）。

`render_footer(state, width)` 是纯函数:中立 `FooterState` → 两行字符串,不碰终端、可单测。
git 分支用 `symbolic-ref`（detached HEAD → None）+ 进程级 TTL 缓存（bottom_toolbar 每次重绘
都会调,不能每次都 fork 一个 git）。token/cwd 格式化对齐 pi `formatTokens`/`formatCwdForFooter`。

构造 `FooterState`（读 agent/manager 私有面）留给调用方（cli.py），本模块只依赖标准库,
保持纯渲染与可测性。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

from prompt_toolkit.utils import get_cwidth


# ─── 格式化原语（移植 pi footer.ts） ──────────────────────────────────────────

def format_tokens(count: int) -> str:
    """紧凑 token 计数:<1k 原样,<1M 用 k,否则 M（pi formatTokens 端口）。"""
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
    """HOME 前缀替换为 ~（pi formatCwdForFooter 端口）。不在 HOME 下则原样绝对路径。"""
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


# ─── git 分支（symbolic-ref + TTL 缓存,移植 pi footer-data-provider.ts 思路） ──

_BRANCH_TTL = 3.0  # 秒;bottom_toolbar 高频重绘,缓存避免每帧 fork git
_branch_cache: dict[str, tuple[float, str | None]] = {}


def git_branch(cwd: str) -> str | None:
    """当前 git 分支名;detached HEAD / 非 git / git 不可用 → None。按 cwd 做 TTL 缓存。"""
    now = time.monotonic()
    hit = _branch_cache.get(cwd)
    if hit is not None and now - hit[0] < _BRANCH_TTL:
        return hit[1]
    branch: str | None = None
    try:
        r = subprocess.run(
            ["git", "--no-optional-locks", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            branch = r.stdout.strip() or None
    except Exception:
        branch = None
    _branch_cache[cwd] = (now, branch)
    return branch


# ─── 状态 + 渲染 ──────────────────────────────────────────────────────────────

@dataclass
class FooterState:
    cwd: str
    home: str | None
    branch: str | None
    session_name: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    context_used: int        # 近似:累计 input（nanocode 不像 pi 有精确 per-turn context usage）
    context_window: int      # effective_window
    model: str
    thinking: str | None     # None=模型不支持/未开;否则展示模式名


def _justify(left: str, right: str, width: int) -> str:
    """左串 + 右对齐右串;放不下则截右串,再放不下只留左串（pi footer 右对齐 model 端口）。"""
    lw = get_cwidth(left)
    rw = get_cwidth(right)
    if lw + 2 + rw <= width:
        return left + " " * (width - lw - rw) + right
    avail = width - lw - 2
    if avail <= 0:
        return left
    # 按显示宽度截断 right
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
    """中立 FooterState → 两行。第 1 行 cwd(branch)•name;第 2 行 token/cost/ctx + 右对齐 model。"""
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

    right = state.model
    if state.thinking:
        right = f"{state.model} • {state.thinking}"

    if width and width > 0:
        stats = _justify(left, right, width) if left else right
    else:
        stats = f"{left}    {right}" if left else right
    return [pwd, stats]
