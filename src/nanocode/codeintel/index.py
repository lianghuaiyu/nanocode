"""codeintel/index.py — RepoIndex：Aider-style 代码库结构感知（docs/15 §9）。

发现：root 即 git toplevel 走 `git ls-files`（吃 .gitignore）,否则 rglob;全量清单 _all_files。
抽取：Python 走 **stdlib AST**（pyast.py：defs 带签名 + refs;语法错回退词法）;其余语言
tree-sitter（可选 extra `codeintel`,vendored *-tags.scm,ts.py）→ 词法回退;
defs-without-refs → Pygments 回填（aider get_tags_raw 同款）。

排名（graph.py,aider get_ranked_tags 复刻）：personal 文件（已读/已改/chat）是**排名种子、
不是输出**——全文已在上下文,map 的价值是顺引用边把「还没读但相关」的文件拉进来;
个性化 PageRank + rank 分发到 (文件, 符号)。

渲染：to_tree（aider 同构）——TreeContext 骨架（Python=treectx.py 复刻;其余语言装了
grep-ast 走真 TreeContext,否则 render_plain）+ 裸文件名,二分搜索拟合 token 预算。

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
              "dist", "build", ".mypy_cache", ".pytest_cache"}


@dataclass
class RepoQuery:
    """repo map 个性化输入（§9.1）。空 query → 仅按 def 密度排名。"""

    chat_files: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    mentioned_files: list[str] = field(default_factory=list)
    mentioned_identifiers: list[str] = field(default_factory=list)


def extract_symbols(rel_path: str, abs_path: str, text: str) -> list[SymbolTag]:
    """抽取一个文件的 symbols。优先级：Python → stdlib AST（限定名+签名,语法错回退词法）；
    其余语言 → tree-sitter（可选 extra,装了即 aider 同款精度）→ 词法回退。
    defs-without-refs → Pygments 回填（aider get_tags_raw 同层同款）。"""
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
    from .ts import extract_ts_symbols
    tags = extract_ts_symbols(rel_path, abs_path, text, lang)
    if tags is not None:                           # tree-sitter 路径（空列表也算成功——aider 语义）
        kinds = {t.kind for t in tags}
        if "def" in kinds and "ref" not in kinds:
            tags = tags + _pygments_refs(rel_path, abs_path, text, lang)
        return tags
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
        self._tree_cache: dict = {}                     # (rel, lois, mtime) → 渲染文本
        self._tree_context_cache: dict = {}             # rel → (mtime, PyTreeContext | None)
        self._all_files: list[str] = []                 # 发现的全量 rel（含非源码,裸文件尾巴用）
        self.truncated = False                          # 语言文件超 max_files,索引被截断
        self._tags_cache = None                         # diskcache 持久层（lazy；冷启动免重解析）

    @property
    def tags_cache(self):
        from .cache import TagsCache
        if self._tags_cache is None:
            self._tags_cache = TagsCache(str(self.root))
        return self._tags_cache

    def update(self, files) -> None:
        """索引给定文件（rel 或 abs path 皆可）。进程内 mtime 未变则跳过；跨进程 tags 走
        diskcache（abs_path+mtime 命中 → 免重 extract_symbols,aider get_tags 语义）。"""
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
                    continue                            # 进程内已是最新
            except Exception:
                continue
            cached = self.tags_cache.get(str(ap), mt)   # 磁盘命中 → 跳过解析（冷启动加速）
            if cached is not None:
                self._tags[rel] = cached
                self._mtime[rel] = mt
                continue
            try:
                text = ap.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            tags = extract_symbols(rel, str(ap), text)
            self._tags[rel] = tags
            self._mtime[rel] = mt
            self.tags_cache.set(str(ap), mt, tags)

    def scan_repo(self, *, max_files: int = 2000) -> None:
        """发现 + 有界索引。git 仓库（root 即 toplevel）走 `git ls-files`——吃 .gitignore、
        无字母序截断偏置（aider 用 tracked files 的同款语义）；非 git 回退 rglob。
        全量发现清单记 _all_files（裸文件尾巴/special 用）；语言文件超过 max_files
        只截断**索引**并打 truncated 标（不静默），发现清单不截。"""
        paths = self._git_files()
        if paths is None:
            paths = [p for p in sorted(self.root.rglob("*"))
                     if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts)]
        self._all_files = []
        self.truncated = False
        count = 0
        for ap in paths:
            self._all_files.append(self._rel(ap))
            if language_for_path(ap.name) is None:
                continue
            if count >= max_files:
                self.truncated = True
                continue
            self.update([ap])
            count += 1

    def _git_files(self) -> "list[Path] | None":
        """root 恰为 git toplevel 时返回 tracked files 的绝对路径；否则 None（回退 rglob）。
        toplevel 校验防两类坑：root 在他人仓库内（ls-files 漏掉 root 自己的文件）、
        临时目录未 track（空清单误判为空仓库）。"""
        import subprocess
        try:
            top = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--show-toplevel"],
                                 capture_output=True, timeout=10)
            if top.returncode != 0:
                return None
            if Path(top.stdout.decode().strip()).resolve() != self.root.resolve():
                return None
            r = subprocess.run(["git", "-C", str(self.root), "ls-files", "-z"],
                               capture_output=True, timeout=15)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        out: list[Path] = []
        for rel in r.stdout.decode("utf-8", errors="replace").split("\0"):
            if not rel or any(part in _SKIP_DIRS for part in Path(rel).parts):
                continue
            ap = self.root / rel
            if ap.is_file():                            # index 里有、盘上已删 → 跳过
                out.append(ap)
        return out

    def all_files(self) -> list[str]:
        """scan_repo 发现的全量 rel path（含非源码文件）。"""
        return list(self._all_files)

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
        token 预算（误差 <15% 提前停）。选择按 rank 序，显示交给 to_tree（其内部 sorted
        ——选择与显示解耦）。"""
        from ..context.packs import estimate_tokens
        if not tags:
            return ""
        self._tree_cache = {}                             # aider：每次 uncached build 重置
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

    def _to_tree(self, tags: list) -> str:
        """aider to_tree 逐字对照：sorted(tags)+dummy 冲洗,def 文件经 TreeContext 渲染骨架,
        裸文件 tuple 只列文件名（无冒号）;末尾全行截断 100 字符。"""
        if not tags:
            return ""

        def _key(t):                                      # Tag 是 namedtuple,混排裸 tuple 可比
            if isinstance(t, tuple):
                return (t[0], 0, -1, "")
            return (t.rel_path, 1, t.line, t.name)

        cur_fname, cur_abs, lois = None, None, None
        output = ""
        dummy = (None,)                                   # 尾部冲洗用
        for tag in sorted(tags, key=_key) + [dummy]:
            this_rel = tag[0] if isinstance(tag, tuple) else tag.rel_path
            if this_rel != cur_fname:
                if lois is not None:
                    output += "\n" + cur_fname + ":\n" + self._render_tree(cur_abs, cur_fname, lois)
                    lois = None
                elif cur_fname:
                    output += "\n" + cur_fname + "\n"
                if not isinstance(tag, tuple):
                    lois = []
                    cur_abs = tag.abs_path
                cur_fname = this_rel
            if lois is not None and not isinstance(tag, tuple):
                lois.append(tag.line)
        # 截断超长行（minified js 之类）——aider 同款收尾
        output = "\n".join(line[:100] for line in output.splitlines()) + "\n"
        return "# Repo map\n" + output

    def _render_tree(self, abs_path: str, rel: str, lois: list[int]) -> str:
        """一个文件的骨架渲染（aider render_tree 等价）：Python → PyTreeContext（父 scope
        头行 + ⋮ 缺口）；其余/解析失败 → render_plain（仅 LOI 行同款格式）。
        (rel, lois, mtime) 级缓存——二分搜索期间同前缀不重渲。"""
        from .treectx import PyTreeContext, render_plain
        try:
            mtime = Path(abs_path).stat().st_mtime
        except OSError:
            return ""
        key = (rel, tuple(sorted(lois)), mtime)
        if key in self._tree_cache:
            return self._tree_cache[key]
        cached = self._tree_context_cache.get(rel)
        if cached is None or cached[0] != mtime:
            ctx = None
            try:
                code = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                if language_for_path(rel) == "python":
                    ctx = PyTreeContext(code)      # stdlib,始终可用
                else:
                    from .ts import tree_context
                    ctx = tree_context(rel, code)  # grep-ast 装了 → 真 TreeContext;否则 None
            except (OSError, SyntaxError, ValueError, RecursionError):
                code = ""
            cached = (mtime, ctx, code)
            self._tree_context_cache[rel] = cached
        _mt, ctx, code = cached
        lois0 = [ln - 1 for ln in lois if ln > 0]         # SymbolTag.line 1-indexed → 0-indexed
        res = ctx.render(lois0) if ctx is not None else render_plain(code, lois0)
        self._tree_cache[key] = res
        return res

    def _rel(self, ap: Path) -> str:
        try:
            return str(ap.resolve().relative_to(self.root))
        except Exception:
            return str(ap)
