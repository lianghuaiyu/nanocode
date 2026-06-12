"""codeintel/index.py — RepoIndex：Aider-style 代码库结构感知（docs/15 §9）。

Phase 4：tree-sitter 未装时用**词法 fallback**（Aider 同款回退）——正则抽 def/ref,简单个性化排名,
预算化渲染。tree-sitter 装上后可在同一 RepoIndex 接口背后替换 symbols 抽取(SymbolTag schema 已统一)。

repo map 不是工具,而是经 RepoMapProvider 注入的 ContextProvider（§9.2）：按任务/已读文件/提及符号
个性化 + 预算封顶。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .symbols import SymbolTag, language_for_path

# 词法 def 正则（按语言族;捕获组 1 = 符号名）。tree-sitter 装上后此表退役。
_DEF_PATTERNS: dict[str, list[str]] = {
    "python": [r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", r"^\s*class\s+([A-Za-z_]\w*)"],
    "javascript": [r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)",
                   r"^\s*(?:export\s+)?class\s+([A-Za-z_]\w*)",
                   r"^\s*(?:export\s+)?const\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\("],
    "go": [r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", r"^\s*type\s+([A-Za-z_]\w*)"],
    "rust": [r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)",
             r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)"],
    "java": [r"^\s*(?:public|private|protected|static|\s)+(?:[\w<>\[\],\s]+\s+)?([A-Za-z_]\w*)\s*\(",
             r"^\s*(?:public|private|protected)?\s*(?:class|interface|enum)\s+([A-Za-z_]\w*)"],
    "ruby": [r"^\s*def\s+([A-Za-z_]\w*[!?]?)", r"^\s*class\s+([A-Za-z_]\w*)",
             r"^\s*module\s+([A-Za-z_]\w*)"],
}
# typescript/tsx 复用 javascript + interface/type；c/cpp/c_sharp/php/swift/kotlin/scala 用通用回退。
_DEF_PATTERNS["typescript"] = _DEF_PATTERNS["javascript"] + [
    r"^\s*(?:export\s+)?interface\s+([A-Za-z_]\w*)", r"^\s*(?:export\s+)?type\s+([A-Za-z_]\w*)"]
_DEF_PATTERNS["tsx"] = _DEF_PATTERNS["typescript"]

_GENERIC_DEF = [r"^\s*(?:public|private|protected|static|func|function|def|fn|class|struct)\s+([A-Za-z_]\w*)"]
_IDENT_RE = re.compile(r"[A-Za-z_]\w{2,}")
_SKIP_DIRS = {".git", ".nanocode", ".claude", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", "_vendor"}


@dataclass
class RepoQuery:
    """repo map 个性化输入（§9.1）。空 query → 仅按 def 密度排名。"""

    chat_files: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    mentioned_files: list[str] = field(default_factory=list)
    mentioned_identifiers: list[str] = field(default_factory=list)


@dataclass
class RankedFile:
    rel_path: str
    score: float
    defs: list[SymbolTag]


def extract_symbols(rel_path: str, abs_path: str, text: str) -> list[SymbolTag]:
    """词法抽取一个文件的 def（按语言正则）。未知语言 → 通用回退。"""
    lang = language_for_path(rel_path)
    if lang is None:
        return []
    patterns = _DEF_PATTERNS.get(lang, _GENERIC_DEF)
    compiled = [re.compile(p) for p in patterns]
    out: list[SymbolTag] = []
    seen: set[tuple[str, int]] = set()
    for i, line in enumerate(text.split("\n"), start=1):
        if len(line) > 400:
            continue
        for rx in compiled:
            m = rx.match(line)
            if m and m.group(1):
                key = (m.group(1), i)
                if key not in seen:
                    seen.add(key)
                    out.append(SymbolTag(rel_path=rel_path, abs_path=abs_path, line=i,
                                         name=m.group(1), kind="def", language=lang))
    return out


class RepoIndex:
    """词法 repo 索引：scan → tags → rank → render。mtime cache 避免重复扫（§9.1 cache）。"""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self._tags: dict[str, list[SymbolTag]] = {}     # rel_path → defs
        self._mtime: dict[str, float] = {}              # rel_path → mtime（cache key）

    def update(self, files) -> None:
        """索引给定文件（rel 或 abs path 皆可）。mtime 未变则跳过（cache）。"""
        for f in files:
            p = Path(f)
            ap = p if p.is_absolute() else (self.root / p)
            try:
                ap = ap.resolve()
                if not ap.is_file():
                    continue
                mt = ap.stat().st_mtime
                rel = self._rel(ap)
                if self._mtime.get(rel) == mt:
                    continue
                text = ap.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            self._tags[rel] = extract_symbols(rel, str(ap), text)
            self._mtime[rel] = mt

    def scan_repo(self, *, max_files: int = 300) -> None:
        """有界扫描 root 下的源文件（跳过 vendor/.git 等;cap 防超大仓库）。"""
        count = 0
        for ap in sorted(self.root.rglob("*")):
            if count >= max_files:
                break
            if any(part in _SKIP_DIRS for part in ap.parts):
                continue
            if not ap.is_file() or language_for_path(ap.name) is None:
                continue
            self.update([ap])
            count += 1

    def tags(self, file: str) -> list[SymbolTag]:
        return self._tags.get(self._rel(Path(file)), [])

    def rank(self, query: RepoQuery) -> list[RankedFile]:
        """个性化排名（词法 lite,非 PageRank）：提及符号 > 提及/已改/已读文件 > def 密度。"""
        mentioned_ids = set(query.mentioned_identifiers)
        personal = {self._rel(Path(f)) for f in
                    (query.chat_files + query.files_modified + query.files_read + query.mentioned_files)}
        ranked: list[RankedFile] = []
        for rel, defs in self._tags.items():
            if not defs:
                continue
            score = 0.0
            if rel in personal:
                score += 10.0
            if any(d.name in mentioned_ids for d in defs):
                score += 25.0
            score += min(len(defs), 20) * 0.5          # def 密度（封顶,避免超大文件霸榜）
            ranked.append(RankedFile(rel_path=rel, score=score, defs=defs))
        ranked.sort(key=lambda r: (-r.score, r.rel_path))
        return ranked

    def render(self, ranked: list[RankedFile], *, budget_tokens: int = 1024,
               max_defs_per_file: int = 12) -> str:
        """预算化渲染（§9.1 budgeted render）：top 文件 + 其 def,累计到 token 预算即截断。"""
        from ..context.packs import estimate_tokens
        lines: list[str] = ["# Repo map (lexical)"]
        shown_files = 0
        for rf in ranked:
            block = [f"\n{rf.rel_path}"]
            for d in rf.defs[:max_defs_per_file]:
                block.append(f"  {d.line:>4}: {d.name}")
            if len(rf.defs) > max_defs_per_file:
                block.append(f"  ... (+{len(rf.defs) - max_defs_per_file} more)")
            candidate = "\n".join(lines + block)
            if estimate_tokens(candidate) > budget_tokens and shown_files > 0:
                lines.append(f"\n[repo map truncated at {budget_tokens} tokens; "
                             f"{len(ranked) - shown_files} more files]")
                break
            lines += block
            shown_files += 1
        return "\n".join(lines)

    def _rel(self, ap: Path) -> str:
        try:
            return str(ap.resolve().relative_to(self.root))
        except Exception:
            return str(ap)
