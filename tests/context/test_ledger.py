"""docs/15 Phase 0：ContextLedger 记账/预算/survival matrix 契约（§8.2/§8.4）。"""

from nanocode.context.ledger import ContextLedger, LedgerEntry
from nanocode.context.packs import ContextPack


def _pack(id, kind, content, *, lifecycle="turn", priority=0, persist="custom_message"):
    return ContextPack(id=id, kind=kind, content=content, lifecycle=lifecycle,
                       priority=priority, persist_policy=persist,
                       provenance={"source": f"{kind}-provider"})


def test_total_tokens_counts_included_only():
    led = ContextLedger()
    led.add(_pack("a", "git", "x" * 40))            # 10 tok
    led.add(_pack("b", "memory", "y" * 80), included=False, reason="dropped")
    assert led.total_tokens() == 10
    assert [p.id for p in led.included_packs()] == ["a"]


def test_by_lifecycle_groups():
    led = ContextLedger()
    led.add(_pack("a", "git", "g", lifecycle="turn"))
    led.add(_pack("b", "proj", "p", lifecycle="session"))
    led.add(_pack("c", "repomap", "r", lifecycle="turn"))
    groups = led.by_lifecycle()
    assert {p.id for p in groups["turn"]} == {"a", "c"}
    assert {p.id for p in groups["session"]} == {"b"}


def test_evict_to_budget_drops_lowest_priority_first():
    led = ContextLedger(budget_tokens=15)
    led.add(_pack("hi", "proj", "x" * 40, priority=10))   # 10 tok, high prio
    led.add(_pack("lo", "repomap", "y" * 40, priority=1))  # 10 tok, low prio
    assert led.over_budget()                               # 20 > 15
    evicted = led.evict_to_budget()
    assert [p.id for p in evicted] == ["lo"]               # 低优先先丢
    assert not led.over_budget()
    assert [p.id for p in led.included_packs()] == ["hi"]


def test_evict_noop_without_budget():
    led = ContextLedger()
    led.add(_pack("a", "k", "x" * 400))
    assert led.evict_to_budget() == []
    assert led.included_packs()


def test_survivors_after_compaction_matrix():
    led = ContextLedger()
    led.add(_pack("session", "proj", "root instructions", lifecycle="session"))
    led.add(_pack("turn", "git", "snapshot", lifecycle="turn"))
    led.add(_pack("until", "skill", "body", lifecycle="until_compact"))
    led.add(_pack("path", "nested", "scoped", lifecycle="path_triggered"))
    survivors = {p.id for p in led.survivors_after_compaction()}
    assert survivors == {"session"}                        # 仅 session 生命周期存活


def test_render_summary_mentions_packs_and_budget():
    led = ContextLedger(budget_tokens=100)
    led.add(_pack("a", "memory", "m" * 40, lifecycle="session"))
    out = led.render_summary()
    assert "memory" in out
    assert "100 budget" in out
    assert "survives" in out                               # session pack survives compaction
    assert "memory-provider" in out                        # provenance source 出现
