"""session/migration.py — P7：把 legacy session 导入 canonical `session.jsonl` 树（docs/13 §10）。

legacy 形态：flat `<sid>.json`（store.save_session）或 v2 `<sid>/main/messages.json`。两者经
`store.load_session` 归一为 `{anthropicMessages|openaiMessages}` provider 列表 → capture 成中立
Message → 写新 `session.jsonl`。**只 append/新建、不删 legacy**；已存在树则跳过（幂等）。

诚实标注（评审 m13）：legacy 快照是 post-注入/post-压缩的 provider 列表，导入会把 ephemeral 注入
当成永久 user 内容（`legacy_import_note`）——best-effort，非逐字忠实。
"""

from __future__ import annotations

from . import capture
from .manager import SessionManager, session_file
from .store import load_session


def _provider_and_messages(data: dict) -> tuple[str, list]:
    a = data.get("anthropicMessages")
    o = data.get("openaiMessages")
    if a:
        return "anthropic", a
    if o:
        return "openai", o
    return "anthropic", []


def migrate_session(session_id: str, *, model: str = "") -> dict:
    """legacy → 树。返回报告 dict（status: migrated|skipped|not_found|empty）。"""
    if SessionManager.exists(session_id):
        return {"session_id": session_id, "status": "skipped", "reason": "session.jsonl already exists"}
    data = load_session(session_id)
    if not data:
        return {"session_id": session_id, "status": "not_found"}
    provider, msgs = _provider_and_messages(data)
    if not msgs:
        return {"session_id": session_id, "status": "empty"}
    neutral = capture.capture_provider_messages(msgs, provider, model=model)
    if not neutral:
        return {"session_id": session_id, "status": "empty"}
    mgr = SessionManager.create(session_id)     # 离线迁移：create 默认持写锁
    try:
        for n in neutral:
            mgr.append_message(n)
    finally:
        mgr.close()                             # 迁移即结束，立即释放写锁
    return {
        "session_id": session_id, "status": "migrated", "provider": provider,
        "messages": len(neutral),
        "legacy_import_note": "legacy snapshot is post-injection/post-compaction; ephemeral "
                              "reminders may be imported as permanent user content (not byte-faithful).",
    }


def inspect_session(session_id: str) -> dict:
    """只读盘点：树是否存在、树 message 数、legacy 是否存在。"""
    has_tree = SessionManager.exists(session_id)
    tree_msgs = 0
    if has_tree:
        from . import tree as _tree
        tree_msgs = sum(1 for e in SessionManager.open(session_id).entries() if e.type == _tree.MESSAGE)
    legacy = load_session(session_id)
    legacy_msgs = 0
    if legacy:
        _, m = _provider_and_messages(legacy)
        legacy_msgs = len(m)
    return {
        "session_id": session_id,
        "tree": {"exists": has_tree, "path": str(session_file(session_id)), "message_entries": tree_msgs},
        "legacy": {"exists": bool(legacy), "messages": legacy_msgs},
    }
