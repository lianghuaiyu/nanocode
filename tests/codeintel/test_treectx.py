"""treectx：grep_ast.TreeContext 的 stdlib-ast 复刻（aider 参数组合下的逐字行为钉点）。"""

from nanocode.codeintel.treectx import PyTreeContext, render_plain

_CODE = '''"""mod doc"""
import os


class Box:
    """doc"""

    def open(
        self,
        key=None,
    ):
        return key

    @staticmethod
    def close():
        pass


def free_fn(a, b):
    return a + b
'''


def test_multiline_signature_expands_with_parent_class():
    ctx = PyTreeContext(_CODE)
    out = ctx.render([7])                      # def open( 的 0-indexed 行
    assert out == ("⋮\n"
                   "│class Box:\n"
                   "⋮\n"
                   "│    def open(\n"
                   "│        self,\n"
                   "│        key=None,\n"
                   "⋮\n")                      # 尾行 ): 排他（grep-ast header range quirk）


def test_decorator_first_line_shown():
    ctx = PyTreeContext(_CODE)
    out = ctx.render([14])                     # def close()
    assert "│    @staticmethod\n│    def close():" in out
    assert "│class Box:" in out                # 父 scope 头行
    assert "pass" not in out                   # 函数体不展示


def test_single_line_def_renders_one_line():
    ctx = PyTreeContext(_CODE)
    out = ctx.render([18])                     # def free_fn(a, b):
    assert out == "⋮\n│def free_fn(a, b):\n⋮\n"


def test_top_of_file_scope_header_suppressed():
    code = "class Top:\n    def m(self):\n        pass\n"
    out = PyTreeContext(code).render([1])      # 只选 method
    assert "│    def m(self):" in out
    assert "│class Top:" not in out            # 首行 scope 不展示（show_top_of_file_parent_scope=False）


def test_close_small_gaps_and_blank_absorb():
    code = "def a():\n    pass\nx = 1\ndef b():\n    pass\n\ny = 2\n"
    ctx = PyTreeContext(code)
    out = ctx.render([0, 3])                   # a 与 b:中间隔 pass/x=1 两行 → 不补
    assert out.count("⋮") >= 1
    out2 = ctx.render([0, 2])                  # 隔 1 行（pass）→ close_small_gaps 补上
    assert "│def a():\n│    pass\n│x = 1\n" in out2


def test_render_plain_lois_only():
    code = "l0\nl1\nl2\nl3\nl4\nl5\n"
    out = render_plain(code, [1, 4])
    assert out == "⋮\n│l1\n⋮\n│l4\n⋮\n"
    # 差 2 的缺口被 close_small_gaps 补上（grep-ast 同款）
    assert render_plain(code, [1, 3]) == "⋮\n│l1\n│l2\n│l3\n⋮\n"


def test_syntax_error_raises_for_caller_fallback():
    import pytest
    with pytest.raises(SyntaxError):
        PyTreeContext("def broken(:\n")
