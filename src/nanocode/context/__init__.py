"""nanocode 上下文工程层（docs/15 §8）—— Claude Code-style request-time context engineering。

L1/L2 之间的横切层：把可用上下文来源按预算 + 缓存策略组装成 ContextPlan。
**不写 session、不调 provider**——只产出 packs + ledger，由 AgentSession 落成 custom_message。

模块：
  packs.py        ContextPack（每个注入源的结构化封装）
  ledger.py       ContextLedger（预算/出处/survival matrix 记账，驱动 /context）
  cache_policy.py prompt cache 稳定性策略
  providers.py    ContextProvider 协议 + 具体 provider（Phase 3）
  budgets.py      BudgetPolicy（Phase 3）
  runtime.py      ContextRuntime（Phase 3）
"""

from .packs import ContextPack, estimate_tokens
from .ledger import ContextLedger, LedgerEntry
from .budgets import BudgetPolicy
from .providers import ContextProvider, ContextRequest, ContextSources, default_providers
from .runtime import ContextRuntime, ContextPlan
from .cache_policy import (
    CACHE_POLICIES,
    PERSIST_POLICIES,
    LIFECYCLES,
    breaks_stable_prefix,
    survives_compaction,
)

__all__ = [
    "ContextPack",
    "estimate_tokens",
    "ContextLedger",
    "LedgerEntry",
    "BudgetPolicy",
    "ContextProvider",
    "ContextRequest",
    "ContextSources",
    "default_providers",
    "ContextRuntime",
    "ContextPlan",
    "CACHE_POLICIES",
    "PERSIST_POLICIES",
    "LIFECYCLES",
    "breaks_stable_prefix",
    "survives_compaction",
]
