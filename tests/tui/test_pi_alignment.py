"""tests/tui/test_pi_alignment.py —— docs:Pi 体验对齐重构的行为锁定。

覆盖:斜杠/@文件补全、工具块(三态背景+diff)、footer ctx% 阈值着色、Shift+Enter 换行、
session 选择器选中行(accent '›' + bold,无反显)。纯单元,不开真 TTY。
"""

from __future__ import annotations

import io
import re

from rich.console import Console

from nanocode.tui.rich_app import RichApp
from nanocode.tui.state import AssistantItem, ToolItem, UserItem


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _console(width: int = 100):
    return Console(file=io.StringIO(), force_terminal=True, width=width)


def _render(app, renderable) -> str:
    con = app._console
    con.print(renderable)
    return con.file.getvalue()


# ── autocomplete ──────────────────────────────────────────────────────────
def _app_with_registry():
    from nanocode.entrypoints.commands.builtin import build_registry
    comp = lambda tok, mode="mention": (
        ["src/nanocode/tui/rich_app.py", "src/nanocode/tui/theme.py"] if mode == "mention"
        else ["src/nanocode/"]
    )
    return RichApp(output=_console(), registry=build_registry(), completer=comp)


def test_slash_menu_filters_commands():
    app = _app_with_registry()
    for ch in "/co":
        app._editor.handle(ch)
    app._update_autocomplete()
    assert app._ac_kind == "command"
    labels = [it["label"] for it in app._ac_items]
    assert any(l == "/cost" for l in labels)
    assert any(l == "/compact" for l in labels)


def test_slash_menu_tab_fills_command():
    app = _app_with_registry()
    for ch in "/comp":
        app._editor.handle(ch)
    app._update_autocomplete()
    app._ac_index = 0
    app._ac_accept(submit=False)
    assert app._editor.text.startswith("/compact")


def test_at_mention_completes_files():
    app = _app_with_registry()
    for ch in "see @theme":
        app._editor.handle(ch)
    app._update_autocomplete()
    assert app._ac_kind == "mention"
    assert any(it["label"].startswith("@") for it in app._ac_items)


def test_no_menu_for_plain_text():
    app = _app_with_registry()
    for ch in "hello world":
        app._editor.handle(ch)
    app._update_autocomplete()
    assert app._ac_kind is None


def test_slash_menu_enter_submits_fully_typed_arg_command():
    """回归:带参命令(arg_hint 非空,如 /sandbox)全名键入后 Enter 应直接提交,而非只填充不跑。"""
    app = _app_with_registry()
    submitted = []
    app._submit = lambda text: submitted.append(text)
    for ch in "/sandbox":
        app._editor.handle(ch)
    app._update_autocomplete()
    assert app._ac_kind == "command"
    app._ac_accept(submit=True)                 # 模拟菜单激活时按 Enter
    assert submitted == ["/sandbox"]
    assert app._ac_kind is None


# ── tool blocks ───────────────────────────────────────────────────────────
def test_tool_box_success_has_diff():
    app = RichApp(output=_console())
    item = ToolItem(id="t1", name="edit_file", input={"file_path": "a.py"}, status="done",
                    result_excerpt="Updated a.py\n+ new line\n- old line")
    out = _render(app, app._render_message_block(item))
    plain = _plain(out)
    assert "Update" in plain and "a.py" in plain      # 标题行
    assert "+1 -1" in plain                            # diff 统计
    assert "new line" in plain and "old line" in plain
    assert "48;2;40;50;40" in out                      # toolSuccessBg 背景填充


def test_tool_box_error_uses_error_bg():
    app = RichApp(output=_console())
    item = ToolItem(id="t2", name="run_shell", input={"command": "ls"}, status="error",
                    result_excerpt="Error: command failed")
    out = _render(app, app._render_message_block(item))
    assert "48;2;60;40;40" in out                      # toolErrorBg 背景填充


def test_user_message_is_filled_box_no_title():
    app = RichApp(output=_console())
    out = _render(app, app._render_message_block(UserItem(text="refactor the parser")))
    plain = _plain(out)
    assert "refactor the parser" in plain
    assert "You" not in plain                          # 无 'You' 标题(Pi 填充色块)
    assert "48;2;52;53;65" in out                      # userMessageBg 填充(fg+bg 可能合并为一条 SGR)


def test_consecutive_tool_blocks_have_no_blank_gap():
    """回归:连续工具块之间不留无背景空行(否则背景「断层」)。"""
    import io
    con = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor", width=60)
    app = RichApp(output=con)

    class _T:
        is_processing = False
        def status(self): return {}
        def state(self): return {}
        def subscribe(self, l): return lambda: None
    app.thread = _T()
    app.state.timeline = [
        ToolItem(id="1", name="list_files", input={"pattern": "*"}, status="done", result_excerpt="a\nb"),
        ToolItem(id="2", name="read_file", input={"file_path": "x.py"}, status="done", result_excerpt="l1\nl2"),
    ]
    app._commit_all_timeline()
    bg = "48;2;40;50;40"
    lines = con.file.getvalue().split("\n")
    bg_idx = [i for i, ln in enumerate(lines) if bg in ln]
    # 所有带背景的行必须连续(无中间空行隔断)
    assert bg_idx, "expected tinted tool lines"
    assert bg_idx == list(range(bg_idx[0], bg_idx[-1] + 1)), "tool background is broken by a gap line"


# ── footer threshold coloring ───────────────────────────────────────────────
def _footer_state(**kw):
    from nanocode.tui.footer import FooterState
    base = dict(cwd="/home/u/p", home="/home/u", branch="main", session_name=None,
                input_tokens=1000, output_tokens=200, cost_usd=0.01,
                context_used=0, context_window=200000, model="opus", thinking=None)
    base.update(kw)
    return FooterState(**base)


def _line2_styles(state):
    from nanocode.tui.footer import render_footer_styled
    line1, line2 = render_footer_styled(state, 80)
    return [(span.style) for span in line2.spans], line2.plain


def test_footer_ctx_pct_green_below_70():
    styles, plain = _line2_styles(_footer_state(context_used=40000))   # 20%
    assert "20%/200k" in plain
    assert "error" not in styles and "warning" not in styles


def test_footer_ctx_pct_warning_over_70():
    styles, plain = _line2_styles(_footer_state(context_used=160000))  # 80%
    assert "80%/200k" in plain
    assert "warning" in styles


def test_footer_ctx_pct_error_over_90():
    styles, plain = _line2_styles(_footer_state(context_used=190000))  # 95%
    assert "95%/200k" in plain
    assert "error" in styles


# ── editor Shift+Enter ──────────────────────────────────────────────────────
def test_shift_enter_inserts_newline_not_submit():
    from nanocode.tui.line_editor import LineEditor
    ed = LineEditor()
    for ch in "abc":
        ed.handle(ch)
    assert ed.handle("shift-enter") is None
    assert ed.text == "abc\n"
    assert ed.handle("enter") == "submit"


# ── theme styles actually apply (regression: compound 'name attr' silently dropped) ────
def test_markdown_heading_is_gold_and_bold():
    app = RichApp(output=_console())
    out = _render(app, app._markdown("# Title\n\n## Sub"))
    assert "38;2;240;198;116" in out          # md_heading gold #f0c674
    assert "1;" in out or "[1m" in out          # bold attribute present


def test_markdown_link_is_blue_underline():
    app = RichApp(output=_console())
    out = _render(app, app._markdown("see [docs](http://x)"))
    assert "38;2;129;162;190" in out          # md_link blue #81a2be
    assert "4;" in out or "[4m" in out          # underline attribute


def test_tool_title_is_bold():
    app = RichApp(output=_console())
    item = ToolItem(id="t", name="read_file", input={"file_path": "x.py"}, status="done",
                    result_excerpt="line1\nline2")
    out = _render(app, app._render_message_block(item))
    # 标题 "Read" 应带 bold(tool_title bundle 了 bold)
    assert re.search(r"\x1b\[[0-9;]*1[;m]", out)


def test_autocomplete_selected_is_accent_bold():
    app = _app_with_registry()
    for ch in "/cost":
        app._editor.handle(ch)
    app._update_autocomplete()
    out = _render(app, app._render_autocomplete())
    assert "→" in _plain(out)                   # 选中行游标
    assert "38;2;138;190;183" in out            # accent #8abeb7


def test_list_item_text_not_accent_colored():
    """回归:列表 marker 上色,但 item 文本保持默认色(曾因 prefix 整行 base style 而全 accent)。"""
    app = RichApp(output=_console())
    out = _render(app, app._markdown("- alpha beta\n- gamma"))
    # 拆出 'alpha beta' 所在片段:它前面应是 reset,不带 accent 前景
    accent = "38;2;138;190;183"
    # accent 只应出现在 marker '- ' 上,'alpha'/'gamma' 文本不应被 accent 包裹
    assert accent in out                         # marker 有 accent
    m = re.search(r"alpha", out)
    seg = out[max(0, m.start() - 12):m.start()]  # 紧邻 'alpha' 之前的字节
    assert accent not in seg                     # 'alpha' 不被 accent 前景着色


def test_h3_heading_has_no_literal_hashes():
    app = RichApp(output=_console())
    out = _plain(_render(app, app._markdown("### Example")))
    assert "Example" in out
    assert "###" not in out                      # 不渲染出字面 '###'


def test_code_block_lines_do_not_fold():
    """回归:代码块每条逻辑行占一行(不折行破坏结构);CJK 注释不被拆到悬挂续行。"""
    con = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor", width=72)
    app = RichApp(output=con)
    md = "```python\nif tc.arguments:\n    acc[i] += tc.arguments  # 拼参数\n```"
    con.print(app._render_message_block(AssistantItem(text=md, complete=True)))
    lines = [l.strip() for l in _plain(con.file.getvalue()).split("\n") if l.strip()]
    assert not any("```" in l for l in lines)            # 不渲染字面 ``` 围栏
    assert any(l == "if tc.arguments:" for l in lines)
    # 含 CJK 注释的整行不被折断:代码与 '# 拼参数' 同处一行
    assert any("acc[i] += tc.arguments" in l and "# 拼参数" in l for l in lines)

    import types
    from nanocode.tui.session_pages.fork import ForkModel
    e = types.SimpleNamespace(type="message", id="x", sessionId="abcdef1234",
                              data={"message": {"role": "user", "content": "hi there"}})
    m = ForkModel([e])
    sel = m.list_text(m.items()[0], True, 60)
    assert "\x1b[7m" not in sel        # 无反显
    assert "›" in sel                  # accent 游标
