"""trajectory._text — 纯叶子文本 helper（Milestone B2：从 trace.redaction 迁出 truncate）。

trajectory 投影/指标/eval 需要的唯一文本整形原语 ``truncate``。原本借 ``trace.redaction.truncate``，
但 ``trace/`` 在 B3 会被删除——故把这一纯函数搬进 trajectory 包内，使 DERIVED 投影层零依赖
``trace.*`` / ``events.*``（三层边界，见 trajectory/__init__.py）。

纯函数、绝不抛（instrumentation 绝不影响调用方）。
"""
from __future__ import annotations


def truncate(text: str, n: int = 1000) -> str:
    """把文本截断到至多 n 字符（超出加省略号标记）。非字符串先 str()。绝不抛。"""
    try:
        s = text if isinstance(text, str) else str(text)
    except Exception:
        return ""
    if len(s) <= n:
        return s
    return s[:n] + f"... [+{len(s) - n} chars]"
