"""context/ledger.py — ContextLedger（docs/15 §8.2）。

回答 `/context` 并驱动预算决策：哪些 pack 在场、各花多少 token、来源、为何包含、何时失效、
是否 survive compaction、是否落进 canonical 树、是否破坏 provider cache。这是 nanocode 今天缺失的
抽象——没有它,上下文质量无法调试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cache_policy import survives_compaction
from .packs import ContextPack


@dataclass
class LedgerEntry:
    """一个 pack 在本次组装中的记账条目。"""

    pack: ContextPack
    included: bool
    reason: str = ""


@dataclass
class ContextLedger:
    """一次上下文组装的全账本。ContextRuntime 填充,AgentSession/CLI 经 render_summary 暴露给 /context。"""

    entries: list[LedgerEntry] = field(default_factory=list)
    budget_tokens: int | None = None

    def add(self, pack: ContextPack, *, included: bool = True, reason: str = "") -> LedgerEntry:
        e = LedgerEntry(pack=pack, included=included, reason=reason)
        self.entries.append(e)
        return e

    def included_packs(self) -> list[ContextPack]:
        return [e.pack for e in self.entries if e.included]

    def total_tokens(self) -> int:
        """已包含 pack 的 token 估计合计。"""
        return sum(e.pack.token_estimate for e in self.entries if e.included)

    def by_lifecycle(self) -> dict[str, list[ContextPack]]:
        out: dict[str, list[ContextPack]] = {}
        for e in self.entries:
            if e.included:
                out.setdefault(e.pack.lifecycle, []).append(e.pack)
        return out

    def over_budget(self) -> bool:
        return self.budget_tokens is not None and self.total_tokens() > self.budget_tokens

    def survivors_after_compaction(self) -> list[ContextPack]:
        """compaction 后仍应存在的 pack（survival matrix）。"""
        return [e.pack for e in self.entries if e.included and survives_compaction(e.pack)]

    def evict_to_budget(self) -> list[ContextPack]:
        """超预算时按 priority 升序（低优先先丢）逐个剔除,直到落进预算。返回被剔除的 pack。

        纯记账操作（把 LedgerEntry.included 翻成 False + 记原因）；不触 session、不调 provider。
        budget 为 None → 无约束,不剔除。
        """
        if self.budget_tokens is None:
            return []
        evicted: list[ContextPack] = []
        # 按 priority 升序遍历已包含项；低 priority 先出局,直到达标。
        for e in sorted((e for e in self.entries if e.included),
                        key=lambda e: (e.pack.priority, e.pack.token_estimate)):
            if self.total_tokens() <= self.budget_tokens:
                break
            e.included = False
            e.reason = f"evicted: over budget ({self.budget_tokens} tok)"
            evicted.append(e.pack)
        return evicted

    def render_summary(self) -> str:
        """`/context` 文本：逐 pack 列 kind / tokens / lifecycle / cache / persist / 是否 survive compaction。"""
        lines = [f"Context ledger — {self.total_tokens()} tokens"
                 + (f" / {self.budget_tokens} budget" if self.budget_tokens else "")]
        for e in self.entries:
            mark = "•" if e.included else "✗"
            surv = "survives" if survives_compaction(e.pack) else "drops"
            src = e.pack.provenance.get("source", "")
            lines.append(
                f"  {mark} {e.pack.kind} [{e.pack.token_estimate} tok] "
                f"lifecycle={e.pack.lifecycle} cache={e.pack.cache_policy} "
                f"persist={e.pack.persist_policy} compaction={surv}"
                + (f" src={src}" if src else "")
                + (f" — {e.reason}" if e.reason else "")
            )
        return "\n".join(lines)
