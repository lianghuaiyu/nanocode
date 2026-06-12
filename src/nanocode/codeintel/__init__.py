"""nanocode codeintel 包（docs/15 §9）—— Aider-style 代码库结构感知。

模块：
  symbols.py   SymbolTag（defs/refs 中立表示，def 带签名行）+ 语言探测
  pyast.py     Python stdlib-AST defs/refs 抽取（语法错回退词法）
  index.py     RepoIndex（update/tags/rank/render；aider 语义：personal=种子不渲染、
               单跳引用加权、预算化渲染）
  service.py   CodeIntelService 门面（嵌入面：与 agent 零耦合；进程级 per-root 缓存）

repo map 不是工具,而是 ContextProvider（§9.2）：经 service 由 RepoMapProvider 注入
per-turn volatile tail；嵌入式 host / SDK 直接调同一 service。tree-sitter / PageRank
是后续升级（docs/16 §5），在同一接口背后替换。
"""

from .symbols import SymbolTag, SUPPORTED_LANGUAGES, language_for_path
from .index import RepoIndex, RepoQuery, extract_symbols
from .service import CodeIntelService, RepoMapResult, get_service, reset_services

__all__ = [
    "SymbolTag", "SUPPORTED_LANGUAGES", "language_for_path",
    "RepoIndex", "RepoQuery", "extract_symbols",
    "CodeIntelService", "RepoMapResult", "get_service", "reset_services",
]
