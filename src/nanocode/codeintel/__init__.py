"""nanocode codeintel 包（docs/15 §9）—— Aider-style 代码库结构感知。

模块（Phase 4 逐步落地）：
  symbols.py   tree-sitter tags：defs/refs 抽取（SymbolTag）—— 本 Phase 0 落地 schema
  index.py     RepoIndex facade（update/tags/rank/render）
  graph.py     依赖图 + PageRank（chat-files/mentioned-files/identifiers 个性化）
  repomap.py   token-budgeted repo-map 渲染
  cache.py     mtime/content-hash cache

repo map 不是工具,而是 ContextProvider（§9.2）：被 ContextRuntime 按任务/已读文件/提及符号/
agent profile 自动注入。
"""

from .symbols import SymbolTag, SUPPORTED_LANGUAGES, language_for_path

__all__ = ["SymbolTag", "SUPPORTED_LANGUAGES", "language_for_path"]
