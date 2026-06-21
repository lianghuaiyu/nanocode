"""tests/tui/test_line_editor.py —— 裸输入层：KeyParser 字节解析 + LineEditor 编辑/历史。"""

from __future__ import annotations

from nanocode.tui.line_editor import KeyParser, LineEditor, PasteToken


# ─── KeyParser ─────────────────────────────────────────────
def test_printable_and_controls():
    p = KeyParser()
    assert p.feed(b"a") == ["a"]
    assert p.feed(b"\r") == ["enter"]
    assert p.feed(b"\n") == ["enter"]
    assert p.feed(b"\x7f") == ["backspace"]
    assert p.feed(b"\x03") == ["ctrl-c"]
    assert p.feed(b"\x04") == ["ctrl-d"]
    assert p.feed(b"\x0f") == ["ctrl-o"]
    assert p.feed(b"\x13") == ["ctrl-s"]


def test_arrow_escape_sequences():
    p = KeyParser()
    assert p.feed(b"\x1b[A") == ["up"]
    assert p.feed(b"\x1b[B") == ["down"]
    assert p.feed(b"\x1b[C") == ["right"]
    assert p.feed(b"\x1b[D") == ["left"]
    assert p.feed(b"\x1b[1;5C") == ["c-right"]
    assert p.feed(b"\x1b[1;5D") == ["c-left"]
    assert p.feed(b"\x1b[1;3C") == ["c-right"]
    assert p.feed(b"\x1b[1;3D") == ["c-left"]
    assert p.feed(b"\x1b[;5C") == ["c-right"]
    assert p.feed(b"\x1b[;5D") == ["c-left"]
    assert p.feed(b"\x1b[1;5:2C") == ["c-right"]
    assert p.feed(b"\x1b[1;5:2D") == ["c-left"]
    assert p.feed(b"\x1bO5C") == ["c-right"]
    assert p.feed(b"\x1bO5D") == ["c-left"]
    assert p.feed(b"\x1b[c") == ["shift-right"]
    assert p.feed(b"\x1b[d") == ["shift-left"]
    assert p.feed(b"\x1bOc") == ["c-right"]
    assert p.feed(b"\x1bOd") == ["c-left"]
    assert p.feed(b"\x1bf") == ["c-right"]
    assert p.feed(b"\x1bb") == ["c-left"]


def test_csi_u_ctrl_letters():
    p = KeyParser()
    assert p.feed(b"\x1b[111;5u") == ["ctrl-o"]
    assert p.feed(b"\x1b[111;5:2u") == ["ctrl-o"]
    assert p.feed(b"\x1b[27;5;111~") == ["ctrl-o"]


def test_sgr_mouse_wheel_sequences():
    p = KeyParser()
    assert p.feed(b"\x1b[<64;10;5M") == ["scrollup"]
    assert p.feed(b"\x1b[<65;10;5M") == ["scrolldown"]


def test_escape_sequence_split_across_feeds():
    p = KeyParser()
    assert p.feed(b"\x1b[") == []      # 不完整 → 暂存
    assert p.feed(b"A") == ["up"]


def test_utf8_multibyte_split_across_feeds():
    p = KeyParser()
    nihao = "你".encode("utf-8")        # 3 bytes
    assert p.feed(nihao[:2]) == []      # 不完整 utf-8 → 等
    assert p.feed(nihao[2:]) == ["你"]


def test_bracketed_paste():
    p = KeyParser()
    out = p.feed(b"\x1b[200~hello\nworld\x1b[201~")
    assert len(out) == 1 and isinstance(out[0], PasteToken)
    assert out[0].text == "hello\nworld"


def test_paste_split_across_feeds():
    p = KeyParser()
    assert p.feed(b"\x1b[200~ab") == []
    out = p.feed(b"cd\x1b[201~")
    assert out == [PasteToken("abcd")]


# ─── LineEditor ────────────────────────────────────────────
def _type(ed, s):
    for ch in s:
        ed.handle(ch)


def test_insert_and_submit():
    ed = LineEditor()
    _type(ed, "hello")
    assert ed.text == "hello" and ed.cursor == 5
    assert ed.handle("enter") == "submit"


def test_lf_enter_submits():
    p = KeyParser()
    ed = LineEditor()
    for token in p.feed(b"hello\n"):
        action = ed.handle(token)
    assert ed.text == "hello"
    assert action == "submit"


def test_backspace_and_cursor():
    ed = LineEditor()
    _type(ed, "abc")
    ed.handle("left")
    assert ed.cursor == 2
    ed.handle("backspace")            # 删 'b'
    assert ed.text == "ac" and ed.cursor == 1


def test_ctrl_j_newline_not_submit():
    ed = LineEditor()
    _type(ed, "a")
    assert ed.handle("ctrl-j") is None
    assert ed.text == "a\n"
    _type(ed, "b")
    assert ed.text == "a\nb"


def test_ctrl_d_eof_only_when_empty():
    ed = LineEditor()
    assert ed.handle("ctrl-d") == "eof"
    _type(ed, "x")
    assert ed.handle("ctrl-d") is None     # 有文本时 ctrl-d 不是 eof


def test_history_recall():
    ed = LineEditor()
    ed.add_history("first")
    ed.add_history("second")
    _type(ed, "draft")
    ed.handle("up")
    assert ed.text == "second"
    ed.handle("up")
    assert ed.text == "first"
    ed.handle("down")
    assert ed.text == "second"
    ed.handle("down")
    assert ed.text == "draft"               # 回到草稿


def test_ctrl_u_kill_line_start():
    ed = LineEditor()
    _type(ed, "hello world")
    ed.handle("ctrl-u")
    assert ed.text == "" and ed.cursor == 0


def test_ctrl_w_delete_word():
    ed = LineEditor()
    _type(ed, "foo bar")
    ed.handle("ctrl-w")
    assert ed.text == "foo "


def test_paste_inserts_text():
    ed = LineEditor()
    ed.handle(PasteToken("a\r\nb"))
    assert ed.text == "a\nb"               # \r\n 归一为 \n


def test_render_has_prompt_and_cursor():
    from rich.console import Console
    ed = LineEditor()
    _type(ed, "hi")
    out = Console(width=40).render_str  # smoke: render returns a Text
    t = ed.render(40)
    assert "hi" in t.plain and t.plain.startswith("> ")
