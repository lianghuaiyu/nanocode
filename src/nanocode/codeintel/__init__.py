"""nanocode codeintel 包（docs/15 §9）—— Aider-style 代码库结构感知。

模块：
  symbols.py   SymbolTag（defs/refs 中立表示，def 带签名行）+ 语言探测
  pyast.py     Python stdlib-AST defs/refs 抽取（语法错回退词法）
  ts.py        tree-sitter 多语言 defs/refs + 真 TreeContext（可选 extra `codeintel`,
               vendored queries/*-tags.scm;没装回退词法/render_plain）
  index.py     RepoIndex（git ls-files 发现/update/tags/rank/render;预算化二分渲染）
  graph.py     个性化 PageRank + rank 分发（aider get_ranked_tags 复刻）
  treectx.py   grep_ast.TreeContext 的 stdlib-ast 复刻（Python 渲染,零依赖）
  special.py   重要文件识别（aider/special.py vendor）
  service.py   CodeIntelService 门面（嵌入面：与 agent 零耦合；进程级 per-root 缓存）

repo map 不是工具,而是 ContextProvider（§9.2）：经 service 由 RepoMapProvider 注入
per-turn volatile tail；嵌入式 host / SDK 直接调同一 service。
"""

from .symbols import SymbolTag, SUPPORTED_LANGUAGES, language_for_path
from .index import RepoIndex, RepoQuery, extract_symbols
from .service import CodeIntelService, RepoMapResult, get_service, reset_services

__all__ = [
    "SymbolTag", "SUPPORTED_LANGUAGES", "language_for_path",
    "RepoIndex", "RepoQuery", "extract_symbols",
    "CodeIntelService", "RepoMapResult", "get_service", "reset_services",
]
