"""tui/theme.py —— 语义配色角色 + 调色板(对位 Pi `theme/theme.ts` + `dark.json`)。

提供两套消费面,源自同一份 DARK 调色板:

1. **Rich Theme**(`rich_theme()`):命名样式,挂到 `primitives.console`。让 `[accent]…[/]` 标记、
   `Text(style="md_heading")`、`Panel(border_style="border")` 等直接引用角色。
2. **裸 ANSI 助手**(`fg/bg/RESET/BOLD/…`):给 session selectors 这类自己拼 ANSI 串、再经
   `Text.from_ansi` 解析的消费者。truecolor SGR;`NO_COLOR` 置空。

颜色取自 Pi 内置 dark 主题(`coding-agent/src/modes/interactive/theme/dark.json`)。仅 DARK
(本仓库用户都是暗色终端);`NO_COLOR` 尊重。不 import 任何 agent/session/tools(嵌入式边界)。
"""

from __future__ import annotations

import os

# ─── 调色板(Pi dark.json)──────────────────────────────────────────
# 语义角色 → hex。前景角色。
_FG = {
    "accent": "#8abeb7",
    "text": "#d4d4d4",
    "muted": "#808080",
    "dim": "#666666",
    "success": "#b5bd68",
    "error": "#cc6666",
    "warning": "#ffff00",
    "thinking_text": "#808080",
    "border": "#5f87ff",
    "border_muted": "#505050",
    "border_accent": "#00d7ff",
    # markdown
    "md_heading": "#f0c674",
    "md_link": "#81a2be",
    "md_link_url": "#666666",
    "md_code": "#8abeb7",
    "md_code_block": "#b5bd68",
    "md_quote": "#808080",
    "md_hr": "#808080",
    "md_list_bullet": "#8abeb7",
    # tools
    "tool_title": "#d4d4d4",
    "tool_output": "#808080",
    "diff_added": "#b5bd68",
    "diff_removed": "#cc6666",
    "diff_context": "#808080",
    # bash mode + thinking gradient
    "bash_mode": "#b5bd68",
    "think_off": "#505050",
    "think_minimal": "#6e6e6e",
    "think_low": "#5f87af",
    "think_medium": "#81a2be",
    "think_high": "#b294bb",
    "think_xhigh": "#d183e8",
}

# 背景角色 → hex。
_BG = {
    "user_message_bg": "#343541",
    "tool_pending_bg": "#282832",
    "tool_success_bg": "#283228",
    "tool_error_bg": "#3c2828",
    "selected_bg": "#3a3a4a",
    "custom_message_bg": "#2d2838",
}

# thinking 档位 → 边框前景角色(动态输入框边框用)。
_THINK_LEVEL = {
    "off": "think_off", "disabled": "think_off", "none": "think_off",
    "minimal": "think_minimal", "low": "think_low",
    "medium": "think_medium", "high": "think_high", "xhigh": "think_xhigh",
}


def no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))


# ─── Rich Theme(命名样式)──────────────────────────────────────────
def _styles() -> dict:
    """role → Rich style 串。fg 角色直接给 hex;再加若干组合填充/属性样式。

    注意:Rich 的 Style.parse 只能把**单个** theme 名解析为样式(`[md_code]` ok),无法把
    theme 名与属性拼用(`"md_heading bold"` 不解析)。故凡需要 bold/italic/underline 的角色,
    都在此把属性 bundle 进样式名,调用方只引用单名。"""
    styles = dict(_FG)
    # 带属性的语义样式(供单名引用)
    styles["md_heading"] = f"bold {_FG['md_heading']}"
    styles["md_heading1"] = f"bold underline {_FG['md_heading']}"
    styles["md_link"] = f"underline {_FG['md_link']}"
    styles["thinking"] = f"italic {_FG['thinking_text']}"
    styles["tool_title"] = f"bold {_FG['tool_title']}"
    styles["ac_selected"] = f"bold {_FG['accent']}"
    # 组合填充样式(前景 on 背景)——工具块/用户块/选中行。
    styles["user_message"] = f"{_FG['text']} on {_BG['user_message_bg']}"
    styles["tool_pending"] = f"on {_BG['tool_pending_bg']}"
    styles["tool_success"] = f"on {_BG['tool_success_bg']}"
    styles["tool_error"] = f"on {_BG['tool_error_bg']}"
    styles["tool_denied"] = f"{_FG['warning']}"
    styles["selected"] = f"{_FG['accent']} on {_BG['selected_bg']}"
    return styles


def rich_theme():
    """构造 Rich Theme;rich 缺席(降级 Console)时返回 None。"""
    try:
        from rich.theme import Theme
    except ModuleNotFoundError:
        return None
    return Theme(_styles(), inherit=True)


# ─── 裸 ANSI 助手(session selectors)─────────────────────────────────
def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def fg(role: str) -> str:
    """role 的前景 truecolor SGR;NO_COLOR → ''。未知 role → ''。"""
    if no_color():
        return ""
    hex_ = _FG.get(role)
    if hex_ is None:
        return ""
    r, g, b = _hex_to_rgb(hex_)
    return f"\x1b[38;2;{r};{g};{b}m"


def bg(role: str) -> str:
    """role 的背景 truecolor SGR;NO_COLOR → ''。未知 role → ''。"""
    if no_color():
        return ""
    hex_ = _BG.get(role)
    if hex_ is None:
        return ""
    r, g, b = _hex_to_rgb(hex_)
    return f"\x1b[48;2;{r};{g};{b}m"


def _attr(code: str) -> str:
    return "" if no_color() else code


RESET = "\x1b[0m"
BOLD = _attr("\x1b[1m")
DIM = _attr("\x1b[2m")
ITALIC = _attr("\x1b[3m")
UNDERLINE = _attr("\x1b[4m")


def thinking_border_role(level: str | None) -> str:
    """thinking 档位 → 边框角色名(动态输入框边框)。未知/关 → border_muted。"""
    if not level:
        return "border_muted"
    return _THINK_LEVEL.get(str(level).lower(), "border_muted")


def hex_of(role: str) -> str | None:
    """role 的 hex(fg 或 bg);供需要 Rich Color 的地方(如 Syntax 背景)取值。"""
    return _FG.get(role) or _BG.get(role)
