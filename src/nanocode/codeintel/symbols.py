"""codeintel/symbols.py — SymbolTag（docs/15 §9.1）+ 语言探测。

tree-sitter tags 的中立表示：一条 def/ref 即一个 SymbolTag。Phase 4 的 symbols 抽取产出这些 tag,
graph 据 referencer→definer 建图 + PageRank,repomap 按预算渲染。本 Phase 0 只落地数据 schema +
语言探测（无 tree-sitter 依赖,后续 Phase 4 接入）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SymbolKind = Literal["def", "ref"]

# 后缀 → 语言名（tree-sitter grammar 名）。Phase 4 扩充。
_EXT_LANGUAGE = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
}

SUPPORTED_LANGUAGES: frozenset[str] = frozenset(_EXT_LANGUAGE.values())


def language_for_path(path: str) -> str | None:
    """按后缀探测语言名;未知后缀 → None（repo map 跳过）。"""
    lower = path.lower()
    for ext, lang in _EXT_LANGUAGE.items():
        if lower.endswith(ext):
            return lang
    return None


@dataclass(frozen=True)
class SymbolTag:
    """一条 tree-sitter tag（§9.1）。frozen + hashable —— 可入 set 去重、作图节点键。

    text：def 所在源行（截断后的签名，aider to_tree 渲染真实代码行的轻量版）；
    ref 不携带（渲染只展示 defs）。additive 字段，缺省空串。"""

    rel_path: str
    abs_path: str
    line: int
    name: str
    kind: SymbolKind
    language: str
    text: str = ""

    @property
    def is_def(self) -> bool:
        return self.kind == "def"

    @property
    def is_ref(self) -> bool:
        return self.kind == "ref"
