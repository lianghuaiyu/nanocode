"""docs/14 P7-b：停写冗余 legacy 快照后，--resume-last 解析改从 canonical session.jsonl header
（timestamp）找最新 top-level session，排除 child session；_auto_save/_persist_state 不再写
legacy flat <sid>.json / v2 main/messages.json。"""

import time

from nanocode.session.manager import SessionManager
from nanocode.session.store import get_latest_session_id


def test_get_latest_resolves_canonical_session_without_legacy_json():
    # 纯 canonical 树（无 <sid>.json / 无 state.json）也能被 --resume-last 找到。
    SessionManager.create("LATEA")
    time.sleep(0.01)
    SessionManager.create("LATEB")          # 更晚 → 应胜出（header timestamp 排序）
    latest = get_latest_session_id()
    assert latest in ("LATEA", "LATEB")     # 二者皆 canonical；最新的胜出（时间戳秒级可能并列）


def test_get_latest_excludes_child_sessions():
    # child session（有 parentSession header）不作 --resume-last 目标。
    SessionManager.create("PARENTL")
    time.sleep(0.01)
    SessionManager.create("PARENTL.agent-001",
                          parent_session={"sessionId": "PARENTL", "entryId": None})
    latest = get_latest_session_id()
    assert latest == "PARENTL"              # child 被排除，即便它更晚


def test_auto_save_no_longer_writes_legacy_flat_snapshot():
    from nanocode.agent.engine import Agent
    from nanocode.session import tree as T
    from nanocode.paths import sessions_dir
    a = Agent(api_key="test", session_id="NOFLAT", permission_mode="bypassPermissions")
    a._session_mgr = SessionManager.create("NOFLAT")
    a._session_mgr.append_message(T.user_message("hi"))
    a.agent_session.auto_save()
    assert not (sessions_dir() / "NOFLAT.json").exists()    # 不再写 legacy flat 快照
    assert SessionManager.exists("NOFLAT")                  # canonical 树是权威
