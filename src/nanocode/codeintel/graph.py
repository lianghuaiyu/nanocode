"""codeintel/graph.py — 文件依赖图 + 个性化 PageRank + rank 分发（aider repomap 复刻）。

逐字对照 aider/repomap.py 的 get_ranked_tags 算法（纯 Python 幂迭代替代 networkx，
同收敛值；无 agent / IO 耦合——嵌入面可独立调用）：

- 节点 = 文件；对每个「既有 def 又有 ref」的 ident，referencer→definer 加边，
  weight = mul × √num_refs；
- mul：mentioned ident ×10；长 snake/kebab/camel 命名（≥8 字符）×10；下划线开头 ×0.1；
  定义出现在 >5 个文件 ×0.1；referencer 是 personal（chat）文件 ×50；
- 只有 def 没有 ref 的 ident → definer 自环 weight 0.1（防图塌）；
  全仓库无任何 ref → references 回退 = defines（自指兜底）；
- personalization：personal / 提及文件 / 路径成分命中提及标识符 → 100/N（dangling 同源）；
- rank 分发：节点 rank 沿出边按权重比例分给 (definer 文件, ident)，按累计值排序——
  这是输出粒度（文件+符号），不是裸文件排名；
- personal 文件**排除出输出**（全文已在上下文）；有 rank 但无入选 def 的文件按节点
  rank 附为裸文件名。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from .index import RepoQuery
from .symbols import SymbolTag

# aider 同值常数
_PERSONALIZE_BASE = 100.0
_PAGERANK_DAMPING = 0.85
_PAGERANK_ITERATIONS = 50
_PAGERANK_TOL = 1e-8


def _ident_multiplier(ident: str, mentioned_idents: set, definer_count: int) -> float:
    """aider 的 ident 权重（repomap.py:486-499 逐字对照）。"""
    mul = 1.0
    is_snake = ("_" in ident) and any(c.isalpha() for c in ident)
    is_kebab = ("-" in ident) and any(c.isalpha() for c in ident)
    is_camel = any(c.isupper() for c in ident) and any(c.islower() for c in ident)
    if ident in mentioned_idents:
        mul *= 10
    if (is_snake or is_kebab or is_camel) and len(ident) >= 8:
        mul *= 10
    if ident.startswith("_"):
        mul *= 0.1
    if definer_count > 5:
        mul *= 0.1
    return mul


def _pagerank(nodes: list[str], edges: list[tuple[str, str, float]],
              personalization: dict[str, float]) -> dict[str, float]:
    """个性化 PageRank（幂迭代；与 networkx.pagerank(weight=..., personalization=...,
    dangling=...) 同语义同收敛）。edges = (src, dst, weight)。"""
    n = len(nodes)
    if n == 0:
        return {}
    # 个性化分布（无 personalization → 均匀）
    if personalization:
        total_p = sum(personalization.values())
        pvec = {u: personalization.get(u, 0.0) / total_p for u in nodes}
    else:
        pvec = {u: 1.0 / n for u in nodes}
    out_weight: dict[str, float] = defaultdict(float)
    out_edges: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for src, dst, w in edges:
        out_weight[src] += w
        out_edges[src].append((dst, w))
    rank = {u: 1.0 / n for u in nodes}
    for _ in range(_PAGERANK_ITERATIONS):
        nxt = {u: 0.0 for u in nodes}
        dangling_mass = 0.0
        for u in nodes:
            r = rank[u]
            ow = out_weight.get(u, 0.0)
            if ow <= 0:
                dangling_mass += r            # dangling 节点的质量按 pvec 重分（aider dangling=pers）
                continue
            for dst, w in out_edges[u]:
                nxt[dst] += r * (w / ow)
        for u in nodes:
            nxt[u] = (1.0 - _PAGERANK_DAMPING) * pvec[u] + _PAGERANK_DAMPING * (
                nxt[u] + dangling_mass * pvec[u])
        err = sum(abs(nxt[u] - rank[u]) for u in nodes)
        rank = nxt
        if err < _PAGERANK_TOL * n:
            break
    return rank


def rank_tags(tags_by_file: dict[str, list[SymbolTag]], query: RepoQuery) -> list:
    """aider get_ranked_tags 的等价实现。

    输入：rel_path → tags（defs+refs）；query 的 personal/提及输入。
    输出：aider 形状的 ranked 列表——SymbolTag（def，按 (文件,符号) 累计 rank 降序）与
    `(rel_path,)` 裸文件 tuple（有 rank/无入选 def 的文件兜底）混合；personal 文件已排除。
    """
    personal = {str(Path(f)) for f in (query.chat_files + query.files_read
                                       + query.files_modified)}
    # personal 集合按 rel 归一（调用方传 rel 或 abs；tags_by_file 键是 rel）
    personal = {p for p in personal} | {Path(p).name for p in personal}

    def _is_personal(rel: str) -> bool:
        return rel in personal or Path(rel).name in personal

    mentioned_idents = set(query.mentioned_identifiers)
    mentioned_files = {str(Path(f)) for f in query.mentioned_files}

    defines: dict[str, set[str]] = defaultdict(set)            # ident → definer files
    definitions: dict[tuple[str, str], list[SymbolTag]] = defaultdict(list)
    references: dict[str, list[str]] = defaultdict(list)       # ident → referencer files
    for rel, tags in tags_by_file.items():
        for t in tags:
            if t.kind == "def":
                defines[t.name].add(rel)
                definitions[(rel, t.name)].append(t)
            elif t.kind == "ref":
                references[t.name].append(rel)
    if not references:                                          # 全仓库无 ref → 自指兜底
        references = {k: list(v) for k, v in defines.items()}

    # personalization（aider repomap.py:422-445：personal/提及文件/路径成分命中提及 ident）
    nodes = sorted(tags_by_file.keys())
    if not nodes:
        return []
    personalize = _PERSONALIZE_BASE / len(nodes)
    personalization: dict[str, float] = {}
    for rel in nodes:
        cur = 0.0
        if _is_personal(rel):
            cur += personalize
        if rel in mentioned_files or Path(rel).name in {Path(m).name for m in mentioned_files}:
            cur = max(cur, personalize)
        p = Path(rel)
        components = set(p.parts) | {p.name, p.stem}
        if components & mentioned_idents:
            cur += personalize
        if cur > 0:
            personalization[rel] = cur

    # 建边（aider repomap.py:472-516）
    edges: list[tuple[str, str, float]] = []
    idents = set(defines) & set(references)
    for ident in defines:                                       # def-only ident → 自环防塌
        if ident in references and ident in idents:
            continue
        for definer in defines[ident]:
            edges.append((definer, definer, 0.1))
    for ident in idents:
        definers = defines[ident]
        mul = _ident_multiplier(ident, mentioned_idents, len(definers))
        for referencer, num_refs in Counter(references[ident]).items():
            use_mul = mul * (50 if _is_personal(referencer) else 1)
            w = use_mul * math.sqrt(num_refs)
            for definer in definers:
                edges.append((referencer, definer, w))

    ranked = _pagerank(nodes, edges, personalization)

    # rank 分发到 (definer, ident)（aider repomap.py:534-543）
    out_by_src: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    for ident in idents:
        definers = defines[ident]
        mul = _ident_multiplier(ident, mentioned_idents, len(definers))
        for referencer, num_refs in Counter(references[ident]).items():
            use_mul = mul * (50 if _is_personal(referencer) else 1)
            w = use_mul * math.sqrt(num_refs)
            for definer in definers:
                out_by_src[referencer].append((definer, w, ident))
    ranked_definitions: dict[tuple[str, str], float] = defaultdict(float)
    for src, outs in out_by_src.items():
        total_w = sum(w for _, w, _ in outs)
        if total_w <= 0:
            continue
        src_rank = ranked.get(src, 0.0)
        for dst, w, ident in outs:
            ranked_definitions[(dst, ident)] += src_rank * w / total_w

    out: list = []
    seen_files: set[str] = set()
    for (rel, ident), _r in sorted(ranked_definitions.items(),
                                   reverse=True, key=lambda kv: (kv[1], kv[0])):
        if _is_personal(rel):
            continue                                            # personal 不渲染（已在上下文）
        out.extend(definitions.get((rel, ident), []))
        seen_files.add(rel)

    # 兜底：有 rank 但无入选 def 的文件按节点 rank 附为裸文件名（aider :558-573）
    for _r, rel in sorted(((r, n) for n, r in ranked.items()), reverse=True):
        if rel not in seen_files and not _is_personal(rel):
            out.append((rel,))
            seen_files.add(rel)
    return out
