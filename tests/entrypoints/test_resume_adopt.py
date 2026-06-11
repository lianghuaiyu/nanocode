"""docs/14 SessionLease：CLI `--resume` 解析 = `get_latest_session_id()`（canonical header 优先），
激活 = `SessionLease.open_or_create` + `cli._load_from_manager`。原 `_resolve_resume_session`
（v2-adopt / flat-json 区分 + `load_session` 读 flat 快照）已退役——canonical 树是唯一 resume 权威。
"""

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.lease import SessionLease
from nanocode.session.manager import SessionManager
from nanocode.session.store import get_latest_session_id
from nanocode.entrypoints.cli import _load_from_manager


def _agent(sid):
    return Agent(api_key="test", trace_enabled=False, session_id=sid, permission_mode="bypassPermissions")


def test_latest_resolves_canonical_session_for_resume():
    SessionManager.create("rtgt").close()
    assert get_latest_session_id() == "rtgt"


def test_resume_activation_loads_latest_canonical_into_agent():
    # --resume 全链路：get_latest → lease open(lock) → _load_from_manager 渲染树进 active 列表。
    m = SessionManager.create("radopt")
    m.append_message(T.user_message("resumed-content"))
    m.close()
    assert get_latest_session_id() == "radopt"
    a = _agent("radopt")
    a._session_mgr = SessionLease.open_or_create("radopt").manager     # cli 激活
    _load_from_manager(a)
    assert "resumed-content" in str(a._anthropic_messages)


def test_no_session_resolves_none():
    assert get_latest_session_id() is None


def test_legacy_only_session_is_not_canonical_resume_target():
    # review high：get_latest_session_id 也会返回 legacy <sid>.json 的 sid（无 canonical 树）。main()
    # --resume 守卫用 SessionManager.exists 区分之 → 拒绝对它 open_or_create（否则会新建空树、静默丢旧
    # 历史 = data loss），改新建全新 session。本测试锚定守卫前提。
    from nanocode.session.store import save_session
    save_session("legonly", {"metadata": {"id": "legonly"},
                             "anthropicMessages": [{"role": "user", "content": "old history"}]})
    assert get_latest_session_id() == "legonly"            # latest 解析到 legacy sid
    assert not SessionManager.exists("legonly")            # 但无 canonical 树 → 守卫触发、不 clobber
