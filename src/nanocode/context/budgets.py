"""context/budgets.py — BudgetPolicy（docs/15 §8.2）。

把散落的 MAX_SESSION_MEMORY_BYTES / SKILL_LISTING_CHAR_BUDGET / MAX_MEMORY_BYTES_PER_FILE /
MAX_RESULT_CHARS 收敛成一个预算权威,供 ContextLedger 做 eviction。token 预算按 model 上下文窗口
的比例派生（与 engine.effective_window 的 0.85 autocompact 阈值对齐思路：上下文工程留足余量）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BudgetPolicy:
    """上下文预算策略。total = 全部 context packs 的 token 上限;per-kind 给关键来源单独封顶。"""

    total_tokens: int = 16000
    repo_map_tokens: int = 1024
    memory_tokens: int = 4000
    skill_listing_tokens: int = 2000

    @classmethod
    def for_window(cls, effective_window: int) -> "BudgetPolicy":
        """按有效上下文窗口派生预算（context packs 总量约窗口的 8%，repo map 约 0.5%）。"""
        total = max(4000, int(effective_window * 0.08))
        return cls(
            total_tokens=total,
            repo_map_tokens=max(512, int(effective_window * 0.005)),
            memory_tokens=max(2000, int(effective_window * 0.02)),
            skill_listing_tokens=max(2000, int(effective_window * 0.02)),
        )

    def cap_for_kind(self, kind: str) -> int | None:
        return {
            "repo_map": self.repo_map_tokens,
            "memory": self.memory_tokens,
            "memory_static": self.memory_tokens,
            "skill_listing": self.skill_listing_tokens,
        }.get(kind)
