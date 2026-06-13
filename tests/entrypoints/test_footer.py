"""footer 纯函数单测（entrypoints/interactive/footer.py）——不碰终端、不 fork git。"""

from __future__ import annotations

from nanocode.entrypoints.interactive.footer import (
    FooterState,
    format_cwd,
    format_tokens,
    render_footer,
)


def test_format_tokens_scales():
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"
    assert format_tokens(1500) == "1.5k"
    assert format_tokens(42100) == "42k"
    assert format_tokens(1_200_000) == "1.2M"


def test_format_cwd_home_replacement(tmp_path):
    home = str(tmp_path)
    assert format_cwd(home, home) == "~"
    sub = str(tmp_path / "proj" / "src")
    assert format_cwd(sub, home) == "~/proj/src"
    # 不在 HOME 下 → 原样绝对路径
    assert format_cwd("/var/log", home) == "/var/log"
    # 无 home → 原样
    assert format_cwd("/x/y", None) == "/x/y"


def _state(**kw) -> FooterState:
    base = dict(
        cwd="/home/u/proj", home="/home/u", branch="main", session_name="重构会话",
        input_tokens=42100, output_tokens=8700, cost_usd=0.21,
        context_used=68000, context_window=200000, model="gpt-5-codex", thinking="adaptive",
    )
    base.update(kw)
    return FooterState(**base)


def test_render_footer_two_lines_full():
    line1, line2 = render_footer(_state())
    assert line1 == "~/proj (main) • 重构会话"
    assert "↑42k" in line2 and "↓8.7k" in line2
    assert "$0.210" in line2
    assert "34.0%/200k" in line2
    assert line2.rstrip().endswith("gpt-5-codex • adaptive")


def test_render_footer_no_branch_no_name():
    line1, _ = render_footer(_state(branch=None, session_name=None))
    assert line1 == "~/proj"


def test_render_footer_thinking_off_shows_model_only():
    _, line2 = render_footer(_state(thinking=None))
    assert line2.rstrip().endswith("gpt-5-codex")
    assert "•" not in line2.split("gpt-5-codex")[-1]


def test_render_footer_zero_usage_omits_token_parts():
    _, line2 = render_footer(_state(input_tokens=0, output_tokens=0, cost_usd=0.0,
                                    context_window=0))
    # 无任何 stats → 只剩 model（右对齐时 left 为空）
    assert "gpt-5-codex" in line2
    assert "↑" not in line2 and "↓" not in line2 and "$" not in line2


def test_render_footer_right_aligns_model_within_width():
    line1, line2 = render_footer(_state(), width=80)
    assert len(line2) <= 80 or line2.endswith("gpt-5-codex • adaptive")
    # model 贴右
    assert line2.rstrip().endswith("gpt-5-codex • adaptive")
