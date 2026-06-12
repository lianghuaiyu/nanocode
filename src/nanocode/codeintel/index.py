"""codeintel/index.py — RepoIndex：Aider-style 代码库结构感知（docs/15 §9）。

抽取：Python 走 **stdlib AST**（pyast.py：defs 带签名 + refs；语法错回退词法）；其余语言词法
defs-only。tree-sitter 装上后可在同一接口背后替换（SymbolTag schema 已统一）。

排名（对照 aider repomap 语义）：personal 文件（已读/已改/chat）是**排名种子、不是输出**——
它们的全文已在上下文里，渲染它们是浪费；map 的价值是顺着引用边把「还没读但相关」的文件拉进来。
单跳引用加权（personal 的 refs → 命中 def 的其他文件加分），mentioned_idents 同时匹配 def 名
与路径成分（aider 的 path-component 技巧）。不做 PageRank（GitHub 级，docs/16 §5 推迟）。

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


def extract_symbols(rel_path: str, abs_path: str, text: str) -> list[SymbolTag]:
    """抽取一个文件的 symbols：Python → AST defs+refs（语法错回退词法）；其余语言词法 defs。"""
    lang = language_for_path(rel_path)
    if lang is None:
        return []
    if lang == "python":
        from .pyast import extract_python_symbols
        try:
            return extract_python_symbols(rel_path, abs_path, text)
        except (SyntaxError, ValueError, RecursionError):
            pass                                   # 语法错/病态文件 → 词法回退（aider 同款降级）
    return _extract_lexical(rel_path, abs_path, text, lang)


def _extract_lexical(rel_path: str, abs_path: str, text: str, lang: str) -> list[SymbolTag]:
    """词法 defs（按语言正则；未知语言通用回退）+ Pygments refs 回填。带签名行（渲染用）。"""
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
                    sig = line.strip()[:120]
                    out.append(SymbolTag(rel_path=rel_path, abs_path=abs_path, line=i,
                                         name=m.group(1), kind="def", language=lang,
                                         text=sig))
    if out:                                            # 有 defs、无 refs → Pygments 回填
        out.extend(_pygments_refs(rel_path, abs_path, text, lang))
    return out


def _pygments_refs(rel_path: str, abs_path: str, text: str, lang: str) -> list[SymbolTag]:
    """aider 同款降级（repomap.py:339-364）：词法路径没有 refs，用 Pygments lexer 的
    Token.Name 回填——使非 Python 文件也参与引用图（referencer 边）。line=-1（无行号语义）。"""
    try:
        from pygments.lexers import guess_lexer_for_filename
        from pygments.token import Token
        lexer = guess_lexer_for_filename(rel_path, text)
    except Exception:
        return []
    out: list[SymbolTag] = []
    seen: set[str] = set()
    try:
        for tok_type, value in lexer.get_tokens(text):
            if tok_type in Token.Name and value not in seen:
                seen.add(value)
                out.append(SymbolTag(rel_path=rel_path, abs_path=abs_path, line=-1,
                                     name=value, kind="ref", language=lang))
    except Exception:
        return []
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

    def scan_repo(self, *, max_files: int = 2000) -> None:
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

    def ranked_tags(self, query: RepoQuery) -> list:
        """aider get_ranked_tags 等价：个性化 PageRank + rank 分发（graph.py），输出
        SymbolTag（def）与 (rel_path,) 裸文件 tuple 的混合列表；personal 文件已排除。
        query 的文件路径归一为 rel（与索引键一致）后入图。"""
        from .graph import rank_tags
        def _rels(files):
            return [self._rel(Path(f)) for f in files]
        q = RepoQuery(
            chat_files=_rels(query.chat_files),
            files_read=_rels(query.files_read),
            files_modified=_rels(query.files_modified),
            mentioned_files=_rels(query.mentioned_files),
            mentioned_identifiers=list(query.mentioned_identifiers),
        )
        return rank_tags(self._tags, q)

    def render_map(self, tags: list, *, budget_tokens: int = 1024) -> str:
        """aider get_ranked_tags_map_uncached 等价：对 ranked tags 数量**二分搜索**拟合
        token 预算（误差 <15% 提前停）。选择按 rank 序，显示按文件名分组（aider to_tree
        先 sorted 再分组——选择与显示解耦）。"""
        from ..context.packs import estimate_tokens
        if not tags:
            return ""
        lower, upper = 0, len(tags)
        best, best_tokens = None, 0
        middle = min(max(budget_tokens // 25, 1), len(tags))
        while lower <= upper:
            tree = self._to_tree(tags[:middle])
            num = estimate_tokens(tree)
            pct_err = abs(num - budget_tokens) / max(budget_tokens, 1)
            if (num <= budget_tokens and num > best_tokens) or pct_err < 0.15:
                best, best_tokens = tree, num
                if pct_err < 0.15:
                    break
            if num < budget_tokens:
                lower = middle + 1
            else:
                upper = middle - 1
            middle = (lower + upper) // 2
        return best or ""

    @staticmethod
    def _to_tree(tags: list) -> str:
        """选中的 tags → 文本（aider to_tree 的轻量版：按文件分组，def 显示签名行；
        裸文件 tuple 只列文件名）。"""
        by_file: dict[str, list] = {}
        bare: list[str] = []
        order: list[str] = []
        for t in tags:
            if isinstance(t, tuple):                  # 裸文件兜底
                if t[0] not in by_file and t[0] not in bare:
                    bare.append(t[0])
                continue
            if t.rel_path not in by_file:
                by_file[t.rel_path] = []
                order.append(t.rel_path)
            by_file[t.rel_path].append(t)
        lines: list[str] = ["# Repo map"]
        for rel in sorted(order):                     # 显示按文件名排序（aider sorted(tags)）
            lines.append(f"\n{rel}:")
            for d in sorted(by_file[rel], key=lambda d: d.line):
                lines.append(f"  {d.line:>4}: {d.text or d.name}")
        for rel in sorted(b for b in bare if b not in by_file):
            lines.append(f"\n{rel}")
        return "\n".join(lines)

    def _rel(self, ap: Path) -> str:
        try:
            return str(ap.resolve().relative_to(self.root))
        except Exception:
            return str(ap)
