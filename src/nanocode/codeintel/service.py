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


@dataclass
class RepoMapResult:
    """一次 repo_map 查询的纯数据结果（嵌入面返回值；text 即注入/展示用文本）。"""

    text: str
    files: list[str] = field(default_factory=list)   # 渲染中实际出现的文件（rel path，排名序）
    token_estimate: int = 0


class CodeIntelService:
    """单 root 的代码结构服务。线程安全（索引更新持锁；查询走索引内存结构）。"""

    def __init__(self, root: str) -> None:
        self.root = str(Path(root).resolve())
        self._index = RepoIndex(self.root)
        self._scanned = False
        self._lock = threading.Lock()

    # ── 索引生命周期 ─────────────────────────────────────────────────────────
    def _ensure_indexed(self, touched: "list[str] | None" = None) -> None:
        with self._lock:
            if not self._scanned:
                self._index.scan_repo()
                self._scanned = True
            if touched:
                self._index.update(touched)          # mtime 未变的文件被 RepoIndex 跳过

    # ── 查询面 ───────────────────────────────────────────────────────────────
    def repo_map(self, query: "RepoQuery | None" = None, *, budget_tokens: int = 1024) -> RepoMapResult:
        """个性化 + 预算化的 repo map（aider 算法：personal 作 PageRank 种子、不渲染；
        rank 分发到 (文件, 符号)；二分拟合预算——见 graph.py / index.render_map）。"""
        from ..context.packs import estimate_tokens
        q = query or RepoQuery()
        self._ensure_indexed(q.chat_files + q.files_read + q.files_modified + q.mentioned_files)
        tags = self._index.ranked_tags(q)
        if not tags:
            return RepoMapResult(text="", files=[], token_estimate=0)
        text = self._index.render_map(tags, budget_tokens=budget_tokens)
        if not text:
            return RepoMapResult(text="", files=[], token_estimate=0)
        shown = sorted({(t[0] if isinstance(t, tuple) else t.rel_path)
                        for t in tags if f"\n{(t[0] if isinstance(t, tuple) else t.rel_path)}" in text})
        return RepoMapResult(text=text, files=shown, token_estimate=estimate_tokens(text))

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


# ─── 进程级 per-root 缓存 ─────────────────────────────────────────────────────

_services: dict[str, CodeIntelService] = {}
_services_lock = threading.Lock()


def get_service(root: str) -> CodeIntelService:
    """取（或建）root 对应的进程级服务实例——索引跨 turn / 跨消费者复用。"""
    key = str(Path(root).resolve())
    with _services_lock:
        svc = _services.get(key)
        if svc is None:
            svc = _services[key] = CodeIntelService(key)
        return svc


def reset_services() -> None:
    """清空进程级缓存（测试 / cwd 大改用）。"""
    with _services_lock:
        _services.clear()
