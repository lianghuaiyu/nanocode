"""tui/line_editor.py —— 裸终端输入层（docs/18 Rich Live 客户端）。

prompt_toolkit 被 Rich Live 取代后，键盘输入要自己接。本模块提供：

- `raw_mode(fd)` / `restore(fd, saved)`：termios cbreak（关 ICANON/ECHO，关 IXON/ICRNL）+ 开启
  bracketed paste（`\x1b[?2004h`）。
- `KeyParser`：把 stdin 原始字节流增量解析成 key token（增量 utf-8 解码——中文输入；转义序列
  方向键；bracketed paste 累积；跨 feed 的不完整序列暂存）。
- `LineEditor`：多行文本 + 光标 + 历史（↑↓ 回溯）。`handle(token)` 返回动作
  （"submit"/"newline"/"cancel"/"eof"/None）；`render(width, prompt)` 出带光标高亮的 Rich Text。

不 import 任何 agent/session/tools——纯输入部件（嵌入式边界）。
"""

from __future__ import annotations

import codecs
import termios
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text


# ─── termios raw mode ──────────────────────────────────────────────
def raw_mode(fd: int):
    """进 cbreak + bracketed paste；返回保存的 termios 属性（传给 restore）。

    用 TCSANOW（立即生效，不等输出 drain）——TCSADRAIN 会阻塞等待 pty 输出排空，在退出收尾时
    （Live 末帧未被对端读走）可能死锁。

    **必须关 ISIG**：否则 Ctrl-C/Ctrl-\\ 由终端生成 SIGINT 信号（asyncio 抛 KeyboardInterrupt 崩进程），
    而非把 `\\x03` 字节交给 reader。本 app 全靠 Ctrl-C 作为字节走优雅 abort，故 ISIG 必关。"""
    saved = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)  # lflag
    new[0] &= ~(termios.IXON | termios.ICRNL | termios.INPCK)  # iflag
    termios.tcsetattr(fd, termios.TCSANOW, new)
    return saved


def restore(fd: int, saved) -> None:
    try:
        termios.tcsetattr(fd, termios.TCSANOW, saved)   # TCSANOW：不等 drain，避免退出时死锁
    except Exception:
        pass


# ─── key parsing ───────────────────────────────────────────────────
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# 转义序列 → token（CSI / SS3 方向键与编辑键；SGR mouse wheel 在 _parse_escape 里单独解析）。
_ESC_SEQS = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "[H": "home", "[F": "end", "OH": "home", "OF": "end",
    "[1~": "home", "[4~": "end", "[3~": "delete",
    "[5~": "pageup", "[6~": "pagedown",
    # Shift+Enter 的多种终端编码(kitty CSI-u / xterm modifyOtherKeys)→ 插入换行
    "[13;2u": "shift-enter", "[27;2;13~": "shift-enter",
}

# 控制字节 → token。
_CTRL = {
    "\r": "enter", "\n": "ctrl-j", "\x7f": "backspace", "\x08": "backspace",
    "\x03": "ctrl-c", "\x04": "ctrl-d", "\x01": "ctrl-a", "\x05": "ctrl-e",
    "\x15": "ctrl-u", "\x0b": "ctrl-k", "\x17": "ctrl-w", "\x09": "tab",
    "\x1b": "escape",
}


@dataclass
class PasteToken:
    text: str


class KeyParser:
    """增量字节流 → token 列表。token 为 str（见 _CTRL/_ESC_SEQS/可打印字符）或 PasteToken。"""

    def __init__(self) -> None:
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""       # 不完整的转义序列残留
        self._in_paste = False
        self._paste_buf = ""

    def feed(self, data: bytes) -> list[Any]:
        s = self._pending + self._dec.decode(data)
        self._pending = ""
        out: list[Any] = []
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if self._in_paste:
                end = s.find(_PASTE_END, i)
                if end == -1:
                    self._paste_buf += s[i:]
                    i = n
                    break
                self._paste_buf += s[i:end]
                out.append(PasteToken(self._paste_buf))
                self._paste_buf = ""
                self._in_paste = False
                i = end + len(_PASTE_END)
                continue
            if ch == "\x1b":
                tok, consumed, incomplete = self._parse_escape(s, i)
                if incomplete:
                    self._pending = s[i:]
                    break
                if tok == "__paste_start__":
                    self._in_paste = True
                    i += consumed
                    continue
                out.append(tok)
                i += consumed
                continue
            if ch in _CTRL:
                out.append(_CTRL[ch])
                i += 1
                continue
            if ch < " " and ch != "\t":   # 其它控制字符忽略
                i += 1
                continue
            out.append(ch)               # 可打印字符（含多字节中文）
            i += 1
        return out

    def _parse_escape(self, s: str, i: int):
        """返回 (token, consumed, incomplete)。i 指向 ESC。"""
        rest = s[i + 1:]
        if rest == "":
            return None, 0, True                         # 只有 ESC，等更多（可能是序列或裸 Esc）
        if s.startswith(_PASTE_START, i):
            return "__paste_start__", len(_PASTE_START), False
        # 不完整的 paste-start 前缀
        if _PASTE_START.startswith(s[i:]):
            return None, 0, True
        c0 = rest[0]
        if c0 in ("\r", "\n"):
            return "shift-enter", 2, False               # ESC+CR(Alt/Shift+Enter)→ 换行
        if c0 not in "[O":
            return "escape", 1, False                    # 裸 Esc（consumed 仅 ESC 本身）
        if rest.startswith("[<"):
            end_m = rest.find("M")
            end_l = rest.find("m")
            candidates = [x for x in (end_m, end_l) if x != -1]
            if not candidates:
                return None, 0, True
            end = min(candidates)
            seq = rest[: end + 1]
            consumed = 1 + len(seq)
            try:
                button = int(seq[2:].split(";", 1)[0])
            except Exception:
                return "escape", consumed, False
            if button & 64:
                direction = button & 3
                if direction == 0:
                    return "scrollup", consumed, False
                if direction == 1:
                    return "scrolldown", consumed, False
            return "mouse", consumed, False
        # CSI/SS3：匹配最长已知序列
        for seq, tok in _ESC_SEQS.items():
            if rest.startswith(seq):
                return tok, 1 + len(seq), False
        # 可能是未完成的序列（如 "[" 后还没来字母/"~"）；若末尾未见终止符则等
        j = 1
        while j < len(rest) and not (rest[j].isalpha() or rest[j] == "~"):
            j += 1
        if j >= len(rest):
            return None, 0, True                         # 序列未完
        return "escape", 1 + j + 1, False                # 未知但完整的序列 → 当 Esc 吞掉

    def pending_is_escape(self) -> bool:
        """残留恰为一个裸 ESC（可能是 Escape 键，也可能是序列起头——需超时裁决）。"""
        return self._pending == "\x1b"

    def flush_escape(self) -> list:
        """超时后把裸 ESC 作为 'escape' 键吐出（终端里 Escape 键 vs 序列靠 inter-byte 计时区分）。"""
        if self._pending == "\x1b":
            self._pending = ""
            return ["escape"]
        return []


# ─── line editor ───────────────────────────────────────────────────
@dataclass
class LineEditor:
    text: str = ""
    cursor: int = 0
    history: list[str] = field(default_factory=list)
    _hist_idx: int | None = None
    _draft: str = ""

    def set_text(self, text: str) -> None:
        self.text = text
        self.cursor = len(text)
        self._hist_idx = None

    def reset(self) -> None:
        self.text = ""
        self.cursor = 0
        self._hist_idx = None

    def add_history(self, line: str) -> None:
        line = line.rstrip("\n")
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)

    def handle(self, token: Any) -> str | None:
        """处理一个 token；返回动作 'submit'/'newline'/'cancel'/'eof' 或 None。"""
        if isinstance(token, PasteToken):
            self._insert(token.text.replace("\r\n", "\n").replace("\r", "\n"))
            return None
        t = token
        if t == "enter":
            # 反斜杠续行:行尾 '\' + Enter → 换行(不提交)
            if self.cursor > 0 and self.text[:self.cursor].endswith("\\"):
                self.text = self.text[: self.cursor - 1] + "\n" + self.text[self.cursor:]
                return None
            return "submit"
        if t in ("ctrl-j", "shift-enter"):
            self._insert("\n")
            return None
        if t == "ctrl-c":
            return "cancel"
        if t == "ctrl-d":
            return "eof" if not self.text else None
        if t == "backspace":
            if self.cursor > 0:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor:]
                self.cursor -= 1
            return None
        if t == "delete":
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1:]
            return None
        if t == "left":
            self.cursor = max(0, self.cursor - 1)
            return None
        if t == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
            return None
        if t in ("home", "ctrl-a"):
            self.cursor = self.text.rfind("\n", 0, self.cursor) + 1
            return None
        if t in ("end", "ctrl-e"):
            nl = self.text.find("\n", self.cursor)
            self.cursor = len(self.text) if nl == -1 else nl
            return None
        if t == "ctrl-u":
            ls = self.text.rfind("\n", 0, self.cursor) + 1
            self.text = self.text[:ls] + self.text[self.cursor:]
            self.cursor = ls
            return None
        if t == "ctrl-k":
            nl = self.text.find("\n", self.cursor)
            end = len(self.text) if nl == -1 else nl
            self.text = self.text[: self.cursor] + self.text[end:]
            return None
        if t == "ctrl-w":
            self._delete_word()
            return None
        if t == "up":
            if "\n" in self.text and self.text.rfind("\n", 0, self.cursor) != -1:
                self._move_line(-1)               # 多行草稿:上移一视觉行(不劫持历史)
            else:
                self._history_prev()
            return None
        if t == "down":
            if "\n" in self.text and self.text.find("\n", self.cursor) != -1:
                self._move_line(1)
            else:
                self._history_next()
            return None
        if isinstance(t, str) and len(t) == 1 and (t >= " " or t == "\t"):
            self._insert(t)
            return None
        return None

    def _insert(self, s: str) -> None:
        self.text = self.text[: self.cursor] + s + self.text[self.cursor:]
        self.cursor += len(s)
        self._hist_idx = None

    def _delete_word(self) -> None:
        i = self.cursor
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        self.text = self.text[:i] + self.text[self.cursor:]
        self.cursor = i

    def _move_line(self, delta: int) -> None:
        """光标按逻辑行上/下移动,尽量保持列位(多行草稿编辑)。"""
        ls = self.text.rfind("\n", 0, self.cursor) + 1   # 当前行起始
        col = self.cursor - ls
        if delta < 0:
            if ls == 0:
                return
            prev_start = self.text.rfind("\n", 0, ls - 1) + 1
            prev_len = (ls - 1) - prev_start
            self.cursor = prev_start + min(col, prev_len)
        else:
            line_end = self.text.find("\n", self.cursor)
            if line_end == -1:
                return
            next_start = line_end + 1
            next_end = self.text.find("\n", next_start)
            if next_end == -1:
                next_end = len(self.text)
            next_len = next_end - next_start
            self.cursor = next_start + min(col, next_len)
        self._hist_idx = None

    def _history_prev(self) -> None:
        if not self.history:
            return
        if self._hist_idx is None:
            self._draft = self.text
            self._hist_idx = len(self.history)
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self.text = self.history[self._hist_idx]
            self.cursor = len(self.text)

    def _history_next(self) -> None:
        if self._hist_idx is None:
            return
        self._hist_idx += 1
        if self._hist_idx >= len(self.history):
            self._hist_idx = None
            self.text = self._draft
        else:
            self.text = self.history[self._hist_idx]
        self.cursor = len(self.text)

    def render(self, width: int, prompt: str = "> ") -> Text:
        """带光标（反显格）的输入显示。"""
        t = Text()
        t.append(prompt, style="bold green")
        text = self.text
        cur = max(0, min(self.cursor, len(text)))
        t.append(text[:cur])
        if cur < len(text):
            t.append(text[cur], style="reverse")
            t.append(text[cur + 1:])
        else:
            t.append(" ", style="reverse")   # 行尾光标块
        return t
