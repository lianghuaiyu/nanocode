"""docs/14 SessionLease：resume = runtime 激活会话写者租约（SessionLease.open with lock）+ 从
canonical 树渲染上下文（cli._load_from_manager）。canonical `session.jsonl` 树是**唯一**权威——
无 flat fallback、无 runtime 自动迁移（离线 `nanocode sessions migrate`）；空树 → 空上下文。
"""

from nanocode.agent.engine import Agent
from nanocode.session import tree, capture
from nanocode.session.manager import SessionManager
from nanocode.session.lease import SessionLease
from nanocode.entrypoints.cli import _load_from_manager

LIVE = [
    {"role": "user", "content": "do it"},
    {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                      {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"p": "x"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "DATA"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
]


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


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


def _seed(sid, provider_msgs):
    mgr = SessionManager.create(sid)        # 持写锁
    for n in capture.capture_provider_messages(provider_msgs, "anthropic", model="claude-x"):
        mgr.append_message(n)
    mgr.close()                             # 释放，供随后 lease open


def test_resume_loads_full_tree_into_active_list():
    _seed("p3sess", LIVE)
    b = _agent("p3sess"); b.model = "claude-x"
    b._session_mgr = SessionLease.open_or_create("p3sess").manager     # 激活写者租约
    _load_from_manager(b)
    assert _norm(b._anthropic_messages) == _norm(LIVE)
    assert sum(1 for e in SessionManager.open("p3sess").entries() if e.type == tree.MESSAGE) == 4


def test_resume_tree_is_sole_authority_ignores_legacy_flat():
    # 树是唯一权威：即便磁盘上有 legacy flat 快照，resume 也只看树（绝不读 flat）。
    from nanocode.session.store import save_session
    save_session("p3auth", {"metadata": {"id": "p3auth"}, "anthropicMessages": list(LIVE)})
    mgr = SessionManager.create("p3auth")
    mgr.append_message(tree.user_message("only-tree")); mgr.close()
    b = _agent("p3auth"); b.model = "claude-x"
    b._session_mgr = SessionLease.open_or_create("p3auth").manager
    _load_from_manager(b)
    assert "only-tree" in str(b._anthropic_messages)
    assert "do it" not in str(b._anthropic_messages)        # legacy flat 内容不出现


def test_resume_empty_tree_yields_empty_context():
    SessionManager.create("p3empty").close()                 # header-only 空树
    b = _agent("p3empty"); b.model = "claude-x"
    b._session_mgr = SessionLease.open_or_create("p3empty").manager
    _load_from_manager(b)
    assert b._anthropic_messages == []                       # 空树 → 空上下文（不静默丢、不回退 flat）
