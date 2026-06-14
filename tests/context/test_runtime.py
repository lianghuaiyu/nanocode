"""docs/15 Phase 3 STEP E：ContextRuntime + ContextProvider 组装契约（§8）。

验收基础：/context 能展示入选 packs + 预算;项目指令/memory 作为 packs（非烤进可变 system）;
survival matrix 可查。这里验证组装层（cutover 到 system prompt 的接线是后续步骤）。
"""

import asyncio

from nanocode.context import (
    BudgetPolicy, ContextRequest, ContextRuntime, ContextPack, ContextSources,
)
from nanocode.context.providers import (
    ContextProvider, EnvProvider, ProjectInstructionsProvider, default_providers,
)


def _run(coro):
    return asyncio.run(coro)


def test_env_provider_emits_pack():
    pack = _run(EnvProvider().collect(ContextRequest(cwd="/tmp/work")))
    assert pack is not None
    assert pack.kind == "env"
    assert "cwd: /tmp/work" in pack.as_text()
    assert pack.persist_policy == "none"          # env 不入树


def test_runtime_collects_enabled_providers_only():
    # 自定义两个 provider：一个永远出 pack,一个被 include 开关关掉。
    class _Always:
        id = "always"
        enable_attr = "include_env"   # 复用一个开关名
        async def collect(self, req):
            return ContextPack(id="always", kind="always", content="A" * 40, priority=10)

    class _Gated:
        id = "gated"
        enable_attr = "include_memory"
        async def collect(self, req):
            return ContextPack(id="gated", kind="gated", content="B" * 40, priority=20)

    rt = ContextRuntime(providers=[_Always(), _Gated()])
    plan = _run(rt.collect(ContextRequest(include_env=True, include_memory=False)))
    ids = [p.id for p in plan.packs]
    assert ids == ["always"]                       # gated provider 被关闭,未参与


def test_runtime_orders_by_priority_desc():
    class _P:
        def __init__(self, id, prio):
            self.id = id; self.enable_attr = "include_env"; self._prio = prio
        async def collect(self, req):
            return ContextPack(id=self.id, kind=self.id, content="x" * 40, priority=self._prio)

    rt = ContextRuntime(providers=[_P("lo", 1), _P("hi", 99), _P("mid", 50)])
    plan = _run(rt.collect(ContextRequest()))
    assert [p.id for p in plan.packs] == ["hi", "mid", "lo"]


def test_runtime_evicts_over_budget_low_priority_first():
    class _P:
        def __init__(self, id, prio):
            self.id = id; self.enable_attr = "include_env"; self._prio = prio
        async def collect(self, req):
            return ContextPack(id=self.id, kind=self.id, content="x" * 400, priority=self._prio)  # 100 tok each

    rt = ContextRuntime(providers=[_P("keep", 90), _P("drop", 5)], budget=BudgetPolicy(total_tokens=120))
    plan = _run(rt.collect(ContextRequest()))
    assert [p.id for p in plan.packs] == ["keep"]   # 低优先 drop 被预算剔除
    # ledger 记录被剔除项 + 原因
    summary = plan.ledger.render_summary()
    assert "drop" in summary and "evicted" in summary


def test_runtime_provider_error_is_recorded_not_fatal():
    class _Boom:
        id = "boom"; enable_attr = "include_env"
        async def collect(self, req):
            raise RuntimeError("kaboom")

    class _OK:
        id = "ok"; enable_attr = "include_env"
        async def collect(self, req):
            return ContextPack(id="ok", kind="ok", content="fine", priority=1)

    rt = ContextRuntime(providers=[_Boom(), _OK()])
    plan = _run(rt.collect(ContextRequest()))
    assert [p.id for p in plan.packs] == ["ok"]      # 错误不破坏组装
    assert "provider error" in plan.ledger.render_summary()


def test_default_providers_cover_the_dynamic_sources():
    ids = {p.id for p in default_providers()}
    assert ids == {"project_instructions", "memory_static", "skills",
                   "agents", "env", "git", "deferred_tools", "repo_map"}


def test_context_sources_are_injected_into_default_providers():
    sources = ContextSources(
        git=lambda req: "git-source",
        project_instructions=lambda req: "project-source",
        memory_static=lambda req: "memory-source",
    )
    rt = ContextRuntime(sources=sources)
    plan = _run(rt.collect(ContextRequest(
        include_env=False,
        include_skills=False,
        include_agents=False,
        include_deferred_tools=False,
        include_repo_map=False,
    )))
    by_kind = {p.kind: p.content for p in plan.packs}
    assert by_kind["git"] == "git-source"
    assert by_kind["project_instructions"] == "project-source"
    assert by_kind["memory_static"] == "memory-source"


def test_project_instructions_pack_survives_compaction():
    # ProjectInstructionsProvider 在有项目指令时产 session-lifecycle pack（§8.4 survives compaction）。
    from nanocode.context.cache_policy import survives_compaction
    p = ProjectInstructionsProvider()
    pack = ContextPack(id="project_instructions", kind="project_instructions",
                       content="do X", lifecycle="session")
    assert survives_compaction(pack)
