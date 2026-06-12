"""codeintel/ts.py — tree-sitter 多语言 defs/refs 抽取 + TreeContext 渲染（可选 extra）。

对照 aider repomap.get_tags_raw / render_tree：tree-sitter 解析 + vendored *-tags.scm
查询（queries/ 目录,取自 aider/queries——上游为各 tree-sitter grammar 仓库的 tags 查询），
捕获 name.definition.* / name.reference.* → SymbolTag；渲染走 grep_ast.TreeContext
（aider 同参数组合）。

**可选依赖**（pyproject extra `codeintel`：grep-ast + tree-sitter-language-pack）——
没装时本模块所有入口返回 None，调用方（index.py）回退词法/render_plain 路径；
装上即得 aider 同款多语言精度。Python 始终走 pyast（限定名 + 零依赖,不经这里）。

嵌入式边界：零 agent import；重依赖由嵌入方按需选装,核心包保持轻。
"""

from __future__ import annotations

from collections import defaultdict
from importlib import resources

from .symbols import SymbolTag

_SIG_MAX_CHARS = 120                       # 与 pyast 一致的签名行截断

# 没有专属 tags 查询、但 grammar 节点类型兼容的语言 → 复用近亲查询
_QUERY_ALIASES = {"tsx": "typescript"}

_TS_READY: "bool | None" = None


def ts_available() -> bool:
    """grep-ast + tree-sitter 语言包是否可用（进程内缓存）。"""
    global _TS_READY
    if _TS_READY is None:
        try:
            import grep_ast  # noqa: F401
            from grep_ast.tsl import get_language, get_parser  # noqa: F401
            from tree_sitter import Query  # noqa: F401
            _TS_READY = True
        except Exception:
            _TS_READY = False
    return _TS_READY


def _query_text(lang: str) -> "str | None":
    name = _QUERY_ALIASES.get(lang, lang)
    try:
        path = resources.files("nanocode.codeintel").joinpath("queries", f"{name}-tags.scm")
        return path.read_text() if path.is_file() else None
    except Exception:
        return None


def _run_captures(query, node) -> dict:
    """py-tree-sitter 0.23（Query.captures）与 0.24+（QueryCursor）双 API（aider 同款），
    归一为 {capture_name: [nodes]}。"""
    if hasattr(query, "captures"):
        res = query.captures(node)
    else:
        from tree_sitter import QueryCursor
        res = QueryCursor(query).captures(node)
    if isinstance(res, dict):
        return res
    by_name = defaultdict(list)                       # 旧 API：list[(node, name)]
    for n, name in res:
        by_name[name].append(n)
    return by_name


def extract_ts_symbols(rel_path: str, abs_path: str, text: str, lang: str) -> "list[SymbolTag] | None":
    """tree-sitter 抽取（aider get_tags_raw 等价）。None → 调用方回退词法。

    成功时返回 tags（可为空——aider 语义：解析成功但无捕获即无 tags，不再回退）；
    defs-without-refs 的回填（Pygments）由调用方做（与 aider 在同一层）。"""
    if not ts_available():
        return None
    scm = _query_text(lang)
    if scm is None:
        return None
    from grep_ast.tsl import get_language, get_parser
    from tree_sitter import Query
    try:
        language = get_language(lang)
        parser = get_parser(lang)
        try:
            tree = parser.parse(bytes(text, "utf-8"))   # 经典 py-tree-sitter（C 绑定）
        except TypeError:
            tree = parser.parse(text)                   # Rust/PyO3 系绑定要 str
        captures = _run_captures(Query(language, scm), tree.root_node)
    except Exception:
        return None
    lines = text.split("\n")
    out: list[SymbolTag] = []
    seen: set[tuple[str, int, str]] = set()
    for capture_name, nodes in captures.items():
        if capture_name.startswith("name.definition."):
            kind = "def"
        elif capture_name.startswith("name.reference."):
            kind = "ref"
        else:
            continue
        for node in nodes:
            row = node.start_point[0]
            name = node.text.decode("utf-8", errors="replace")
            key = (name, row, kind)
            if not name or key in seen:
                continue
            seen.add(key)
            sig = ""
            if kind == "def" and 0 <= row < len(lines):
                raw = lines[row].strip()
                sig = raw[:_SIG_MAX_CHARS] + ("…" if len(raw) > _SIG_MAX_CHARS else "")
            out.append(SymbolTag(rel_path=rel_path, abs_path=abs_path, line=row + 1,
                                 name=name, kind=kind, language=lang, text=sig))
    return out


def tree_context(rel_path: str, code: str):
    """grep_ast.TreeContext（aider render_tree 的参数组合），包成与 PyTreeContext 同形的
    `.render(lois_0indexed)` 适配器。不可用/未知语言/解析失败 → None（回退 render_plain）。"""
    if not ts_available():
        return None
    from grep_ast import TreeContext
    try:
        tc = TreeContext(
            rel_path, code if code.endswith("\n") else code + "\n",
            color=False, line_number=False, child_context=False, last_line=False,
            margin=0, mark_lois=False, loi_pad=0, show_top_of_file_parent_scope=False)
    except Exception:
        return None
    return _TsContext(tc)


class _TsContext:
    def __init__(self, tc) -> None:
        self._tc = tc

    def render(self, lois: list[int]) -> str:
        tc = self._tc
        tc.lines_of_interest = set()                  # aider render_tree 同款复用方式
        tc.add_lines_of_interest(lois)
        tc.add_context()
        return tc.format()
