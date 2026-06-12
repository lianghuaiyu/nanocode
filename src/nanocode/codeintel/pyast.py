"""codeintel/pyast.py — Python 的 AST defs/refs 抽取（docs/15 §9 / repo map 升级第一步）。

stdlib `ast`，零新依赖——这是「词法 → 结构」的中间档：比正则准确（嵌套函数/方法/装饰器下
不漏不错），又刻意不上 tree-sitter/PageRank（那是 GitHub 级升级，见 docs/16 §5 推迟项）。

产出统一的 SymbolTag：
- def：module 级与嵌套的 function / async function / class；方法另发一条 `Class.method`
  限定名（display/点查友好），裸名那条供跨文件 ref 匹配。def 携带源行签名（text）。
- ref：Load 上下文的 Name、Attribute 的 attr 名、import 进来的名字——单跳引用排名
  （index.rank）据此把「personal 文件引用到的定义文件」拉高。

SyntaxError / 解析失败 → 调用方回退词法抽取（extract_symbols 内部处理），这里直接抛。
"""

from __future__ import annotations

import ast

from .symbols import SymbolTag

# 渲染签名行的截断上限（防 minified/超长行撑爆 map）。
_SIG_MAX_CHARS = 120

# ref 噪音过滤：内置名/伪引用不进 ref 集（匹配不到任何项目内 def，徒增体积）。
_REF_NOISE = frozenset({
    "self", "cls", "True", "False", "None",
    "print", "len", "range", "str", "int", "float", "bool", "dict", "list", "set",
    "tuple", "type", "isinstance", "getattr", "setattr", "hasattr", "super",
    "Exception", "ValueError", "TypeError", "RuntimeError", "KeyError",
})


def extract_python_symbols(rel_path: str, abs_path: str, text: str) -> list[SymbolTag]:
    """AST 抽取一个 Python 文件的 defs + refs。语法错误由调用方回退词法——这里直接抛。"""
    tree = ast.parse(text)
    lines = text.split("\n")
    out: list[SymbolTag] = []
    seen: set[tuple[str, int, str]] = set()

    def _sig(line: int) -> str:
        raw = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        return raw[:_SIG_MAX_CHARS] + ("…" if len(raw) > _SIG_MAX_CHARS else "")

    def add(name: str, line: int, kind: str, *, sig: bool = False) -> None:
        if not name or (kind == "ref" and name in _REF_NOISE):
            return
        key = (name, line, kind)
        if key in seen:
            return
        seen.add(key)
        out.append(SymbolTag(rel_path=rel_path, abs_path=abs_path, line=line,
                             name=name, kind=kind, language="python",
                             text=_sig(line) if sig else ""))

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._class_stack: list[str] = []

        # ── defs ──
        def _visit_func(self, node) -> None:
            add(node.name, node.lineno, "def", sig=True)
            if self._class_stack:
                add(f"{self._class_stack[-1]}.{node.name}", node.lineno, "def", sig=True)
            self.generic_visit(node)

        visit_FunctionDef = _visit_func
        visit_AsyncFunctionDef = _visit_func

        def visit_ClassDef(self, node) -> None:
            add(node.name, node.lineno, "def", sig=True)
            self._class_stack.append(node.name)
            self.generic_visit(node)
            self._class_stack.pop()

        # ── refs ──
        def visit_Name(self, node) -> None:
            if isinstance(node.ctx, ast.Load):
                add(node.id, node.lineno, "ref")
            self.generic_visit(node)

        def visit_Attribute(self, node) -> None:
            add(node.attr, node.lineno, "ref")
            self.generic_visit(node)

        def visit_Import(self, node) -> None:
            for alias in node.names:
                add((alias.asname or alias.name).split(".")[0], node.lineno, "ref")

        def visit_ImportFrom(self, node) -> None:
            for alias in node.names:
                if alias.name != "*":
                    add(alias.asname or alias.name, node.lineno, "ref")

    _Visitor().visit(tree)
    return out
