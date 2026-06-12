"""docs/14 SessionLease：resume = runtime 激活会话写者租约（SessionLease.open with lock）+ 从
canonical 树按轮重渲染请求（agent_session.build_request_messages，docs/16 #3c：flat 装载已退役）。
canonical `session.jsonl` 树是**唯一**权威——无 flat fallback、无 runtime 自动迁移；空树 → 空上下文。
"""

from nanocode.agent.engine import Agent
from nanocode.session import tree, capture
from nanocode.session.manager import SessionManager
from nanocode.session.lease import SessionLease

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
    assert _norm(b.agent_session.build_request_messages()) == _norm(LIVE)
    assert sum(1 for e in SessionManager.open("p3sess").entries() if e.type == tree.MESSAGE) == 4


def test_resume_tree_is_sole_authority_ignores_stray_files():
    # 树是唯一权威：即便 sessions 目录里有杂散的同名 .json 文件，resume 也只看树（docs/16 C-3：
    # flat 读写器已删，此处直接落一个杂散文件钉住"绝不读 flat"性质）。
    import json
    from nanocode.paths import sessions_dir
    (sessions_dir() / "p3auth.json").write_text(
        json.dumps({"metadata": {"id": "p3auth"}, "anthropicMessages": list(LIVE)}, default=str))
    mgr = SessionManager.create("p3auth")
    mgr.append_message(tree.user_message("only-tree")); mgr.close()
    b = _agent("p3auth"); b.model = "claude-x"
    b._session_mgr = SessionLease.open_or_create("p3auth").manager
    req = b.agent_session.build_request_messages()
    assert "only-tree" in str(req)
    assert "do it" not in str(req)                          # legacy flat 内容不出现


def test_resume_empty_tree_yields_empty_context():
    SessionManager.create("p3empty").close()                 # header-only 空树
    b = _agent("p3empty"); b.model = "claude-x"
    b._session_mgr = SessionLease.open_or_create("p3empty").manager
    assert b.agent_session.build_request_messages() == []   # 空树 → 空上下文（不静默丢、不回退 flat）
