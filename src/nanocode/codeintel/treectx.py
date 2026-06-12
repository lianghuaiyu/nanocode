"""codeintel/treectx.py — grep_ast.TreeContext 的 Python stdlib-ast 复刻（repo map 渲染层）。

aider 的 to_tree 用 TreeContext（tree-sitter）把每个入选 def 渲染成「带结构上下文的代码骨架」：
LOI 行本体 + 各级父 scope 的 header 行，缺口处一个 `⋮`，展示行加 `│` 前缀。本模块在
**aider 的固定参数组合**（color=False, line_number=False, parent_context=True,
child_context=False, last_line=False, margin=0, mark_lois=False, loi_pad=0,
show_top_of_file_parent_scope=False, header_max=10）下逐字复刻 grep_ast/grep_ast.py 的行为，
解析器换成 stdlib ast——Python 文件零依赖即得 aider 同款渲染。

与 grep-ast 对齐的关键语义（quirk 也保留）：
- scopes[i] = 覆盖第 i 行的所有节点的起始行集合；header 候选只收 size>0 的节点；
- header 解析:候选 **>1 个**取最小节点 (size,start,end)（size>10 截到 start+10），
  否则退化为 (i, i+1)——单行展示；range(start,end) **尾行排他**；
- 起始于文件首行的 scope 不展示 header（show_top_of_file_parent_scope=False）；
- close_small_gaps：i 与 i+2 都展示则补 i+1；展示的非空行后紧跟空行则吸附空行；
- format：未展示段打一个 `⋮`，展示行 `│` 前缀；首行未展示则以 `⋮` 开头。

tree-sitter 没有的节点用合成 span 等价：decorated_definition →（首个装饰器行..def 末行）、
parameters →（def 行..body 前一行，仅多行签名时）。

非 Python / 语法错文件由调用方走 render_plain（仅 LOI 行 + 同款缺口/前缀格式）。
"""

from __future__ import annotations

import ast
from collections import defaultdict

_HEADER_MAX = 10                       # grep-ast TreeContext(header_max=10) 默认值


def _close_and_format(lines: list[str], show: set[int], num_lines: int) -> str:
    """grep-ast 的 close_small_gaps + format（aider 参数下）。show 为 0-indexed 行集合。"""
    if not show:
        return ""
    closed = set(show)
    ss = sorted(show)
    for a, b in zip(ss, ss[1:]):                          # i 与 i+2 展示 → 补 i+1
        if b - a == 2:
            closed.add(a + 1)
    for i in sorted(closed):                              # 非空展示行后紧跟空行 → 吸附
        if (i < len(lines) and lines[i].strip()
                and i < num_lines - 2 and not lines[i + 1].strip()):
            closed.add(i + 1)
    out: list[str] = []
    dots = 0 not in closed
    for i, line in enumerate(lines):
        if i not in closed:
            if dots:
                out.append("⋮")
                dots = False
            continue
        out.append(f"│{line}")
        dots = True
    return "\n".join(out) + "\n"


def render_plain(code: str, lois: list[int]) -> str:
    """无结构解析时的回退渲染：只展示 LOI 行，缺口/前缀格式与 TreeContext 一致。
    lois 为 0-indexed。"""
    lines = code.splitlines()
    show = {i for i in lois if 0 <= i < len(lines)}
    return _close_and_format(lines, show, len(lines) + 1)


class PyTreeContext:
    """一个 Python 文件的可复用渲染上下文（解析一次，render 多次——二分搜索期间反复调用）。

    构造可能抛 SyntaxError / RecursionError——调用方回退 render_plain。"""

    def __init__(self, code: str) -> None:
        tree = ast.parse(code)                            # 可抛——调用方处理
        self.lines = code.splitlines()
        self.num_lines = len(self.lines) + 1
        # scopes[i] = 覆盖第 i 行的节点起始行集合（grep-ast walk_tree 同构,0-indexed）
        self.scopes: list[set[int]] = [set() for _ in range(self.num_lines)]
        cands: dict[int, list[tuple[int, int, int]]] = defaultdict(list)

        def _add_span(s: int, e: int) -> None:
            if s >= self.num_lines:
                return
            if e > s:                                     # size==0 不进 header 候选
                cands[s].append((e - s, s, e))
            for i in range(s, min(e, self.num_lines - 1) + 1):
                self.scopes[i].add(s)

        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None)
            if lineno is None or end is None:
                continue
            s, e = lineno - 1, end - 1
            _add_span(s, e)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.decorator_list:                   # 合成 decorated_definition
                    deco = min(d.lineno for d in node.decorator_list) - 1
                    _add_span(deco, e)
                if node.body:                             # 合成 parameters（多行签名）
                    sig_end = node.body[0].lineno - 1 - 1
                    if sig_end > s:
                        _add_span(s, sig_end)

        # header 解析（grep-ast __init__ 后处理逐字对照,含 len>1 的 quirk）
        self.header: list[tuple[int, int]] = []
        for i in range(self.num_lines):
            header = sorted(cands.get(i, []))
            if len(header) > 1:
                size, head_start, head_end = header[0]
                if size > _HEADER_MAX:
                    head_end = head_start + _HEADER_MAX
            else:
                head_start, head_end = i, i + 1
            self.header.append((head_start, head_end))

    def render(self, lois: list[int]) -> str:
        """LOI（0-indexed）→ 骨架文本。等价 grep-ast 的
        add_lines_of_interest + add_context + format（aider 参数:仅 parent_context）。"""
        lois_set = {i for i in lois if 0 <= i < len(self.lines)}
        if not lois_set:
            return ""
        show = set(lois_set)
        for i in lois_set:                                # add_parent_scopes（无 last_line 递归）
            if i >= len(self.scopes):
                continue
            for line_num in self.scopes[i]:
                head_start, head_end = self.header[line_num]
                if head_start > 0:                        # 首行 scope 不展示 header
                    show.update(range(head_start, head_end))
        return _close_and_format(self.lines, show, self.num_lines)
