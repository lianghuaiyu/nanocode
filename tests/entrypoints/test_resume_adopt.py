"""docs/14 SessionLease：CLI `--resume` 解析 = `get_latest_session_id()`（canonical header），
激活 = `SessionLease.open_or_create`（请求随后按轮从树重渲染，docs/16 #3c）。docs/16 C-3：
legacy flat/v2 发现面已删——canonical 树是唯一 resume 权威，latest 必有树。
"""

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.lease import SessionLease
from nanocode.session.manager import SessionManager
from nanocode.session.store import get_latest_session_id


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def test_latest_resolves_canonical_session_for_resume():
    SessionManager.create("rtgt").close()
    assert get_latest_session_id() == "rtgt"


def test_resume_activation_loads_latest_canonical_into_agent():
    # --resume 全链路：get_latest → lease open(lock) → 请求按轮从树重渲染。
    m = SessionManager.create("radopt")
    m.append_message(T.user_message("resumed-content"))
    m.close()
    assert get_latest_session_id() == "radopt"
    a = _agent("radopt")
    a._session_mgr = SessionLease.open_or_create("radopt").manager     # cli 激活
    assert "resumed-content" in str(a.agent_session.build_request_messages())


def test_no_session_resolves_none():
    assert get_latest_session_id() is None
