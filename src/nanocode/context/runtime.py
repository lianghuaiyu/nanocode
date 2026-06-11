"""context/runtime.py — ContextRuntime（docs/15 §8）。

L1/L2 之间的横切层：把可用上下文来源（ContextProvider）按预算 + 缓存策略组装成 ContextPlan
（packs + ledger）。**不写 session、不调 provider(LLM)**——只产出计划,由 AgentSession 落成
custom_message entry（§8/§4 注释）。

用法：
    plan = await ContextRuntime().collect(ContextRequest(cwd=..., include_memory=...))
    plan.packs   # 入选的 ContextPack(按 priority 降序)
    plan.ledger  # ContextLedger(记账 + /context render + survival matrix)
"""

from __future__ import annotations

from dataclasses import dataclass

from .budgets import BudgetPolicy
from .ledger import ContextLedger
from .packs import ContextPack
from .providers import ContextProvider, ContextRequest, default_providers


@dataclass
class ContextPlan:
    """一次组装的结果：入选 packs（priority 降序）+ 完整 ledger（含被剔除项与原因）。"""

    packs: list[ContextPack]
    ledger: ContextLedger


class ContextRuntime:
    """组装上下文计划。无状态;providers 可注入（测试/profile 定制）。"""

    def __init__(self, providers: "list[ContextProvider] | None" = None,
                 budget: "BudgetPolicy | None" = None) -> None:
        self.providers = providers if providers is not None else default_providers()
        self.budget = budget or BudgetPolicy()

    async def collect(self, request: ContextRequest) -> ContextPlan:
        ledger = ContextLedger(budget_tokens=self.budget.total_tokens)
        for p in self.providers:
            enabled = getattr(request, getattr(p, "enable_attr", ""), True)
            if not enabled:
                continue  # profile 关闭该来源 → 跳过（不记账,避免 /context 噪声）
            try:
                pack = await p.collect(request)
            except Exception as e:  # provider 失败绝不破坏组装（best-effort,记账可见）
                ledger.add(ContextPack(id=p.id, kind=getattr(p, "id", "?"), content="",
                                       provenance={"source": type(p).__name__, "error": str(e)}),
                           included=False, reason=f"provider error: {e}")
                continue
            if pack is None:
                continue  # 无内容（空 git/空 memory…）
            # per-kind 预算封顶（仅记账提示;真正 eviction 由 ledger.evict_to_budget 统一做）
            cap = self.budget.cap_for_kind(pack.kind)
            reason = f"from {p.id}"
            if cap is not None and pack.token_estimate > cap:
                reason += f" (over per-kind cap {cap})"
            ledger.add(pack, reason=reason)
        ledger.evict_to_budget()
        # 入选按 priority 降序（高优先靠前组装,稳定前缀在前）。
        packs = sorted(ledger.included_packs(), key=lambda p: -p.priority)
        return ContextPlan(packs=packs, ledger=ledger)
