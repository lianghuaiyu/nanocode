"""codeintel/service.py — CodeIntelService：代码结构服务门面（嵌入面，docs/15 §9）。

与 agent **零耦合**：不 import agent/session/engine，输入是纯 RepoQuery、输出是纯数据——
这是可嵌入 agent 理念的体现点：内置 host（RepoMapProvider）是它的第一个消费者，
嵌入式 host / SDK / 未来「model 发起的 repo_map 工具」（tool plane 那扇门）直接调同一门面。

进程级 per-root 缓存：首次访问做有界 scan_repo，之后按 RepoQuery 触碰的文件增量 update
（RepoIndex 内部 mtime 失效）——跨 turn 复用索引，不再每次重建（修掉原 provider 每 call
新建 RepoIndex 的成本 bug）。

骨架边界（刻意不做）：磁盘持久化缓存 / 文件 watch / tree-sitter / PageRank——GitHub 级。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from .index import RepoIndex, RepoQuery
from .symbols import SymbolTag

_REFRESH_MODES = frozenset({"auto", "files", "always", "manual"})


@dataclass
class RepoMapResult:
    """一次 repo_map 查询的纯数据结果（嵌入面返回值；text 即注入/展示用文本）。"""

    text: str
    files: list[str] = field(default_factory=list)   # 渲染中实际出现的文件（rel path，排名序）
    token_estimate: int = 0
    truncated: bool = False                          # 索引被 max_files 截断（覆盖不全,不静默）


class CodeIntelService:
    """单 root 的代码结构服务。线程安全（索引更新持锁；查询走索引内存结构）。"""

    def __init__(self, root: str, *, refresh: str = "auto") -> None:
        self.root = str(Path(root).resolve())
        self._index = RepoIndex(self.root)
        self._scanned = False
        self._lock = threading.Lock()
        # aider RepoMap refresh 四档 + map 结果缓存
        self.refresh = refresh if refresh in _REFRESH_MODES else "auto"
        self._map_cache: dict = {}
        self._last_map: "RepoMapResult | None" = None
        self._map_processing_time = 0.0

    # ── 索引生命周期 ─────────────────────────────────────────────────────────
    def _ensure_indexed(self, touched: "list[str] | None" = None) -> None:
        with self._lock:
            if not self._scanned:
                self._index.scan_repo()
                self._scanned = True
            if touched:
                self._index.update(touched)          # mtime 未变的文件被 RepoIndex 跳过

    # ── 查询面 ───────────────────────────────────────────────────────────────
    def repo_map(self, query: "RepoQuery | None" = None, *, budget_tokens: int = 1024,
                 refresh: "str | None" = None, force_refresh: bool = False) -> RepoMapResult:
        """个性化 + 预算化的 repo map。map 结果缓存 + refresh 四档（aider
        get_ranked_tags_map 复刻）包在 _repo_map_uncached 外：

        - manual：有 last_map 直接返回（永不重算,除非 force）；
        - always：从不用缓存；
        - files：总是用缓存（key 含 personal 文件 + budget,文件没变即命中）；
        - auto：仅当上次构建 >1s（map_processing_time）才用缓存——便宜的小仓库每次重算
          保证新鲜,贵的大仓库吃缓存。auto 档 cache_key 额外纳入 mentioned（aider 同款）。"""
        import time
        q = query or RepoQuery()
        mode = refresh or self.refresh
        key = self._map_cache_key(q, budget_tokens, mode)
        if not force_refresh:
            if mode == "manual" and self._last_map is not None:
                return self._last_map
            use_cache = (mode == "files"
                         or (mode == "auto" and self._map_processing_time > 1.0))
            if use_cache and key in self._map_cache:
                return self._map_cache[key]
        start = time.time()
        result = self._repo_map_uncached(q, budget_tokens)
        self._map_processing_time = time.time() - start
        self._map_cache[key] = result
        self._last_map = result
        return result

    @staticmethod
    def _map_cache_key(q: "RepoQuery", budget_tokens: int, mode: str) -> tuple:
        """aider get_ranked_tags_map cache_key 等价：personal 文件 + budget;auto 档纳入
        mentioned（refresh!=auto 时 mention 变化不触发重算——aider 同款取舍）。"""
        base = (tuple(sorted(q.chat_files)), tuple(sorted(q.files_read)),
                tuple(sorted(q.files_modified)), budget_tokens)
        if mode == "auto":
            base += (tuple(sorted(q.mentioned_files)), tuple(sorted(q.mentioned_identifiers)))
        return base

    def _repo_map_uncached(self, q: "RepoQuery", budget_tokens: int) -> RepoMapResult:
        """组装与 aider get_ranked_tags(_map_uncached) 同构：图排名 tags 之后接**全量发现
        文件**的裸文件尾巴（含 README 等非源码——预算大时可见），special 文件前置过滤
        因尾巴已含全部文件而为 no-op（aider 源码同款行为，保真保留）。"""
        from ..context.packs import estimate_tokens
        from .special import filter_important_files
        self._ensure_indexed(q.chat_files + q.files_read + q.files_modified + q.mentioned_files)
        tags = self._index.ranked_tags(q)
        personal = {self._index._rel(Path(f))
                    for f in (q.chat_files + q.files_read + q.files_modified)}
        included = {(t[0] if isinstance(t, tuple) else t.rel_path) for t in tags}
        # aider get_ranked_tags 尾部：其余已发现文件按裸文件名附加（确定序）
        others = sorted(rel for rel in set(self._index.all_files())
                        if rel not in included and rel not in personal)
        tags = tags + [(rel,) for rel in others]
        included |= set(others)
        # aider get_ranked_tags_map_uncached 的 special 前置（同款过滤,见 docstring）
        special = [fn for fn in filter_important_files(others) if fn not in included]
        tags = [(fn,) for fn in special] + tags
        if not tags:
            return RepoMapResult(text="", files=[], token_estimate=0)
        text = self._index.render_map(tags, budget_tokens=budget_tokens)
        if not text:
            return RepoMapResult(text="", files=[], token_estimate=0)
        shown = sorted({(t[0] if isinstance(t, tuple) else t.rel_path)
                        for t in tags if f"\n{(t[0] if isinstance(t, tuple) else t.rel_path)}" in text})
        return RepoMapResult(text=text, files=shown, token_estimate=estimate_tokens(text),
                             truncated=self._index.truncated)

    def defs(self, file: str) -> list[SymbolTag]:
        """一个文件的定义（function/class/method；Python 带签名行）。"""
        self._ensure_indexed([file])
        return [t for t in self._index.tags(file) if t.kind == "def"]

    def refs(self, file: str) -> list[SymbolTag]:
        """一个文件引用到的符号（Python AST；非 Python 词法 defs-only → 空）。"""
        self._ensure_indexed([file])
        return [t for t in self._index.tags(file) if t.kind == "ref"]

    def def_names(self) -> set[str]:
        """全仓库已知定义名集合（提及提取 / 嵌入面点查用）。"""
        self._ensure_indexed()
        return {t.name for tags in self._index._tags.values()
                for t in tags if t.kind == "def"}

    def files(self) -> set[str]:
        """已索引文件的 rel path 集合。"""
        self._ensure_indexed()
        return set(self._index._tags.keys())

    def extract_mentions(self, text: str) -> "tuple[list[str], list[str]]":
        """从自然语言文本提取 (mentioned_identifiers, mentioned_files)——aider
        get_ident_mentions/get_file_mentions 的等价实现：分词后与已知 def 名 / 文件名求交。
        放在 service（非 agent）：嵌入式 host 喂任意文本即可获得同款个性化。"""
        import re as _re
        if not text:
            return [], []
        words = set(_re.findall(r"[A-Za-z_][\w.\-/]{2,}", text))
        names = self.def_names()
        idents = sorted(w for w in words if w in names)
        files = self.files()
        by_base = {}
        for rel in files:
            by_base.setdefault(Path(rel).name, rel)
            by_base.setdefault(Path(rel).stem, rel)
        mfiles = sorted({by_base[w] for w in words if w in by_base}
                        | {w for w in words if w in files})
        return idents, mfiles

    def find_definition(self, name: str) -> list[SymbolTag]:
        """跨文件点查：name 的全部定义（裸名或 Class.method 限定名）。
        是未来「model 发起的 repo_map 工具」的现成后端。"""
        self._ensure_indexed()
        out: list[SymbolTag] = []
        for tags in self._index._tags.values():
            out.extend(t for t in tags if t.kind == "def" and t.name == name)
        out.sort(key=lambda t: (t.rel_path, t.line))
        return out

    def close(self) -> None:
        """释放底层 diskcache 句柄（进程退出/reset 时）。"""
        self._index.tags_cache.close()


# ─── 进程级 per-root 缓存 ─────────────────────────────────────────────────────

_services: dict[str, CodeIntelService] = {}
_services_lock = threading.Lock()


def get_service(root: str) -> CodeIntelService:
    """取（或建）root 对应的进程级服务实例——索引跨 turn / 跨消费者复用。
    refresh 默认 auto;消费者（RepoMapProvider）按 settings 在 repo_map(refresh=) per-call 覆盖
    ——service 不读任何 agent/tools 配置,保持嵌入面零反向依赖。"""
    key = str(Path(root).resolve())
    with _services_lock:
        svc = _services.get(key)
        if svc is None:
            svc = _services[key] = CodeIntelService(key)
        return svc


def reset_services() -> None:
    """清空进程级缓存（测试 / cwd 大改用）；先 close 各 diskcache 句柄。"""
    with _services_lock:
        for svc in _services.values():
            try:
                svc.close()
            except Exception:
                pass
        _services.clear()
