"""docs/26 G4：before_compact 策略的扩展宿主侧契约（注册单 owner + 弃权 + 归一）。

host.run_compaction_strategy 与 orchestration 的不同点：**无 fail-loud**——压缩必须始终前进，
无注册即弃权（CompactionOutcome(None, cancel=False)），内核回退内置 summarizer。
"""
import asyncio

import pytest

from nanocode.agent.compaction import CompactionOutcome, CompactionRequest
from nanocode.extensions import ExtensionHost
from nanocode.extensions.api import ExtensionAPI
from nanocode.extensions.errors import ExtensionLoadError
from nanocode.extensions.registry import ContributionRegistry


class _Thread:
    """create_context 所需的最小 thread（_agent=None → tasks/events 槽为 None）。"""
    _agent = None
    model = "claude-x"

    def readonly_session(self):
        return None


def _bound_host() -> ExtensionHost:
    h = ExtensionHost([])
    h._activated = True
    h.bind_runtime(_Thread(), None)
    return h


# ─── registry / api 注册契约 ──────────────────────────────────────────────────

def test_single_owner_dup_fails_loud():
    reg = ContributionRegistry()

    async def s(ctx, request):
        return CompactionOutcome()

    reg.add_compaction_strategy(s, extension_id="a")
    with pytest.raises(ExtensionLoadError):
        reg.add_compaction_strategy(s, extension_id="b")


def test_api_register_wires_into_registry():
    reg = ContributionRegistry()
    api = ExtensionAPI(reg, extension_id="ext1")

    async def s(ctx, request):
        return CompactionOutcome()

    api.register_compaction_strategy(s)
    assert reg.compaction_strategy is not None
    assert reg.compaction_strategy[1] == "ext1"


# ─── host.run_compaction_strategy 分派 ────────────────────────────────────────

def test_no_strategy_abstains():
    h = _bound_host()
    out = asyncio.run(h.run_compaction_strategy(CompactionRequest()))
    assert isinstance(out, CompactionOutcome)
    assert out.summary is None and out.cancel is False


def test_registered_strategy_outcome_used():
    h = _bound_host()
    seen = {}

    async def strat(ctx, request):
        seen["req"] = request
        return CompactionOutcome(summary="EXT_SUMMARY")

    h.registry.add_compaction_strategy(strat, extension_id="x")
    req = CompactionRequest(messages=[{"role": "user", "content": "hi"}], tokens_before=42)
    out = asyncio.run(h.run_compaction_strategy(req))
    assert out.summary == "EXT_SUMMARY" and out.cancel is False
    # ctx 是 call-time 构建的；request 原样传入（curated 投影）。
    assert seen["req"].tokens_before == 42


def test_strategy_can_cancel():
    h = _bound_host()

    async def strat(ctx, request):
        return CompactionOutcome(cancel=True)

    h.registry.add_compaction_strategy(strat, extension_id="x")
    out = asyncio.run(h.run_compaction_strategy(CompactionRequest()))
    assert out.cancel is True


def test_non_outcome_return_normalized_to_abstain():
    h = _bound_host()

    async def strat(ctx, request):
        return {"summary": "wrong shape"}  # 非 CompactionOutcome → 归一为弃权

    h.registry.add_compaction_strategy(strat, extension_id="x")
    out = asyncio.run(h.run_compaction_strategy(CompactionRequest()))
    assert isinstance(out, CompactionOutcome)
    assert out.summary is None and out.cancel is False
