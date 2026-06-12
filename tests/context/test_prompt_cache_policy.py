"""docs/15 Phase 0：prompt cache 稳定性策略（§8.3）+ 「packs 作 custom_message 追加,绝不改写
last user 消息」的可执行契约（§8.5）。后者直接打到真实 session 管线上验证。
"""

from nanocode.context.cache_policy import breaks_stable_prefix, survives_compaction
from nanocode.context.packs import ContextPack
from nanocode.session import tree
from nanocode.session.manager import SessionManager
from nanocode.session.render import ModelCtx, render

ANTH = ModelCtx(provider="anthropic", api="anthropic", model_id="claude-x")


def test_stable_prefix_pack_with_session_lifecycle_does_not_break():
    packs = [ContextPack(id="sys", kind="identity", content="stable",
                         cache_policy="stable_prefix", lifecycle="session")]
    assert breaks_stable_prefix(packs) is False


def test_misclassified_volatile_in_stable_prefix_breaks():
    # 自称进 stable_prefix 但 lifecycle=turn（每轮变）→ 击穿前缀缓存
    packs = [ContextPack(id="bad", kind="git", content="snapshot",
                         cache_policy="stable_prefix", lifecycle="turn")]
    assert breaks_stable_prefix(packs) is True


def test_append_only_and_volatile_tail_never_break_prefix():
    packs = [
        ContextPack(id="a", kind="memory", content="m", cache_policy="append_only", lifecycle="turn"),
        ContextPack(id="b", kind="repomap", content="r", cache_policy="volatile_tail", lifecycle="turn"),
    ]
    assert breaks_stable_prefix(packs) is False


def test_survives_compaction_classification():
    assert survives_compaction(ContextPack(id="s", kind="proj", content="x", lifecycle="session"))
    assert not survives_compaction(ContextPack(id="t", kind="git", content="x", lifecycle="turn"))
    assert not survives_compaction(ContextPack(id="u", kind="skill", content="x", lifecycle="until_compact"))
    assert not survives_compaction(ContextPack(id="p", kind="nested", content="x", lifecycle="path_triggered"))
    assert not survives_compaction(ContextPack(id="o", kind="skill_body", content="x", lifecycle="one_shot"))


def test_pack_injected_as_separate_custom_message_not_mutating_user():
    """§8.5：注入作为独立 custom_message entry,绝不 in-place 改写 last user 消息。"""
    mgr = SessionManager.create(cwd="/tmp")
    mgr.append_message(tree.user_message("original user text"))
    pack = ContextPack(id="m1", kind="memory", content="REMEMBER: x", persist_policy="custom_message")
    mgr.append(tree.CUSTOM_MESSAGE, pack.to_custom_message())

    # user MESSAGE entry 原文未被改写
    msg_entries = [e for e in mgr.entries() if e.type == tree.MESSAGE]
    assert msg_entries[-1].data["message"]["content"] == "original user text"
    # custom_message 是独立 entry
    cm = [e for e in mgr.entries() if e.type == tree.CUSTOM_MESSAGE]
    assert len(cm) == 1 and cm[0].data["content"] == "REMEMBER: x"

    # render：原 user 文本 + pack 文本都在；pack 原样无 PREFIX（custom_message 不被改写,§8.5）
    out = render(mgr.build_context().messages, ANTH)["messages"]
    flat = str(out)
    assert "original user text" in flat
    assert "REMEMBER: x" in flat
