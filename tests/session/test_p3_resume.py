"""P3：resume 走 build_context（树优先 + 快照降级兜底，docs/13 §9-P3 / 评审 M3）。

验证：① 有树 → restore_session 从树重建、续聊等价；② 树比 legacy 短（compaction 缺口）→ 回退
legacy，无 resume 数据丢失；③ 无树 → 既有行为不变；④ resume_from_tree 隔离单元。
"""

import json

from nanocode.agent.engine import Agent
from nanocode.session import tree
from nanocode.session.manager import SessionManager
from nanocode.session.resume_from_tree import resume_from_tree

LIVE = [
    {"role": "user", "content": "do it"},
    {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                      {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"p": "x"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "DATA"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
]


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


def _norm(msgs):
    """归一 anthropic provider 列表用于等价比较（tool_result is_error 缺省抹平）。"""
    out = []
    for m in msgs:
        c = m.get("content")
        if m["role"] == "assistant":
            out.append(("a", [(b.get("type"), b.get("text"), b.get("id"), b.get("name"), b.get("input"))
                              for b in c]))
        elif isinstance(c, list) and c and c[0].get("type") == "tool_result":
            out.append(("tr", [(b["tool_use_id"], b["content"], bool(b.get("is_error", False))) for b in c]))
        else:
            out.append(("u", c))
    return out


# ─── resume_from_tree 隔离单元 ────────────────────────────────────────────────
def test_resume_from_tree_none_when_no_tree():
    assert resume_from_tree("nope", provider="anthropic", model="claude-x") is None


def test_resume_from_tree_renders_tree():
    mgr = SessionManager.create("p3t")
    mgr.append_message(tree.user_message("hi"))
    mgr.append_message(tree.assistant_message([tree.text_block("yo")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    got = resume_from_tree("p3t", provider="anthropic", model="claude-x")
    assert [m["role"] for m in got] == ["user", "assistant"]


# ─── restore_session：树优先 ──────────────────────────────────────────────────
def test_restore_prefers_tree_when_complete():
    # 完整 turn 落进树（S1 后由 message-end 写；此处直接 seed 一棵完整树）
    from nanocode.session import capture
    mgr = SessionManager.create("p3sess")
    for n in capture.capture_provider_messages(LIVE, "anthropic", model="claude-x"):
        mgr.append_message(n)
    # B 同 session resume：data 给快照；树完整 → 应从树重建，续聊等价
    b = _agent("p3sess")
    b.model = "claude-x"
    b.restore_session({"anthropicMessages": list(LIVE)})
    assert _norm(b._anthropic_messages) == _norm(LIVE)
    # 确实来自树：树里有 4 条 message entry
    assert sum(1 for e in SessionManager.open("p3sess").entries() if e.type == tree.MESSAGE) == 4


def test_restore_uses_tree_authority_even_if_shorter_than_legacy():
    # docs/14 §4.2：树是 resume 唯一权威。树比 legacy 快照短（如压缩后）→ 用树，不再回退快照。
    mgr = SessionManager.create("p3short")
    mgr.append_message(tree.user_message("partial1"))
    mgr.append_message(tree.assistant_message([tree.text_block("partial2")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    b = _agent("p3short")
    b.model = "claude-x"
    b.restore_session({"anthropicMessages": list(LIVE)})  # 快照 4 条，但树只有 2 条
    roles = [m["role"] for m in b._anthropic_messages]
    assert roles == ["user", "assistant"]                 # 用了树（2），非快照（4）
    assert "partial1" in str(b._anthropic_messages)
    assert "done" not in str(b._anthropic_messages)       # 快照独有内容不出现


def test_restore_auto_migrates_legacy_to_tree():
    # docs/14 §4.2：无 canonical 树但有盘上 legacy 快照 → restore 自动迁移建树再从树重建。
    from nanocode.session.store import save_session
    save_session("p3mig", {"metadata": {"id": "p3mig"}, "anthropicMessages": list(LIVE)})
    assert not SessionManager.exists("p3mig")
    b = _agent("p3mig")
    b.model = "claude-x"
    b.restore_session({"anthropicMessages": list(LIVE)})
    assert SessionManager.exists("p3mig")                 # 迁移建了 canonical 树
    assert "do it" in str(b._anthropic_messages)


def test_restore_no_tree_no_legacy_uses_inmemory_snapshot_fallback():
    # 无树、盘上无 legacy（迁移无果）→ 末路兜底装入 data 的 flat 列表（P7 删 legacy 后消失）。
    b = _agent("p3none")
    b.model = "claude-x"
    b.restore_session({"anthropicMessages": list(LIVE)})
    assert _norm(b._anthropic_messages) == _norm(LIVE)
