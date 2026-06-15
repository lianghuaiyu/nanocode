"""docs/18 Phase 8/9：session notes 接口预留 + repo map volatile 边界（survival matrix）。

Phase 8 是**留桩**——只验证契约形状与未接线；Phase 9 的 repo-map 行为边界（map_tokens=0、no-files
multiplier、不落树、不算 readFiles）已分别由 tests/codeintel/test_repomap.py、
tests/agent/test_turn_context_cutover.py、tests/session/test_compaction_details.py 覆盖，这里补一条
survival-matrix 层的结构边界锁。
"""

from nanocode.context import session_notes as sn
from nanocode.context.cache_policy import CACHE_POLICIES, LIFECYCLES, PERSIST_POLICIES, survives_compaction
from nanocode.context.packs import ContextPack
from nanocode.context.providers import default_providers


# ── Phase 8：session_notes 预留契约 ────────────────────────────────────────────
def test_session_notes_contract_uses_existing_vocab():
    assert sn.SESSION_NOTES_KIND == "session_notes"
    # lifecycle/persist/cache 都用已有词表（落地无需改 schema）
    assert sn.SESSION_NOTES_LIFECYCLE in LIFECYCLES
    assert sn.SESSION_NOTES_PERSIST_POLICY in PERSIST_POLICIES
    assert sn.SESSION_NOTES_CACHE_POLICY in CACHE_POLICIES
    # 预算上限（docs/18 §8）
    assert sn.SESSION_NOTES_TOTAL_TOKENS == 12_000
    assert sn.SESSION_NOTES_SECTION_TOKENS == 2_000
    assert len(sn.SESSION_NOTES_SECTIONS) >= 8


def test_session_notes_provider_is_reserved_not_implemented():
    import pytest
    with pytest.raises(NotImplementedError):
        sn.SessionNotesProvider()


def test_session_notes_not_wired_into_default_providers():
    ids = {getattr(p, "id", None) for p in default_providers()}
    assert sn.SESSION_NOTES_KIND not in ids       # 桩未接线进任何 turn 路径


def test_session_notes_pack_would_survive_compaction():
    # 按预留契约构造的 pack：lifecycle=session → 跨 compaction 存活（与 project_instructions 同级）
    pack = ContextPack(id="session_notes", kind=sn.SESSION_NOTES_KIND, content="...",
                       lifecycle=sn.SESSION_NOTES_LIFECYCLE,
                       cache_policy=sn.SESSION_NOTES_CACHE_POLICY,
                       persist_policy=sn.SESSION_NOTES_PERSIST_POLICY)
    assert survives_compaction(pack) is True


# ── Phase 9：repo map 是 volatile turn-tail，绝不跨 compaction 存活 ─────────────
def test_repo_map_pack_never_survives_compaction():
    repo_map_pack = ContextPack(id="repo_map", kind="repo_map", content="# Repo map\n...",
                                lifecycle="turn", cache_policy="volatile_tail", persist_policy="none")
    assert survives_compaction(repo_map_pack) is False     # turn-volatile：压缩后不恢复（不污染 summary）
