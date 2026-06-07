"""Task 7: CLI _resolve_resume_session — v2 采纳 session_id + state。

- v2 session → (original_id, data with state & v2=True)
- 旧 flat JSON → (None, data without state)
- 空 → (None, None)
"""

from nanocode.entrypoints import cli as _cli_mod
from nanocode.session import v2 as _session_v2


def test_resolve_resume_v2_adopts_session_id(monkeypatch, tmp_path):
    """v2 session: returns (session_id, data) with state and v2=True."""
    sid = "v2sess01"
    # Setup: write v2 state to make is_v2_session true
    monkeypatch.setattr(_session_v2, "session_root", lambda s: tmp_path / s)
    state = {"tasks": [], "subagents": [], "task_seq": 0, "agent_seq": 0}
    _session_v2.write_state(sid, state)

    # Stub the imports at the cli module level
    monkeypatch.setattr(_cli_mod, "get_latest_session_id", lambda: sid)
    monkeypatch.setattr(_cli_mod, "load_session", lambda _sid: {
        "anthropicMessages": [{"role": "user", "content": "hi"}],
    })

    adopt_sid, data = _cli_mod._resolve_resume_session()
    assert adopt_sid == sid
    assert data["v2"] is True
    assert data["state"] == state
    assert data["anthropicMessages"] == [{"role": "user", "content": "hi"}]


def test_resolve_resume_flat_json_no_adopt(monkeypatch, tmp_path):
    """Flat JSON session: returns (None, data) without session_id adoption."""
    sid = "flatsess"
    monkeypatch.setattr(_session_v2, "session_root", lambda s: tmp_path / s)
    # No v2 state file → flat path

    monkeypatch.setattr(_cli_mod, "get_latest_session_id", lambda: sid)
    monkeypatch.setattr(_cli_mod, "load_session", lambda _sid: {
        "anthropicMessages": [{"role": "user", "content": "flat"}],
    })

    adopt_sid, data = _cli_mod._resolve_resume_session()
    assert adopt_sid is None
    assert data is not None
    assert data.get("state") is None
    assert data["anthropicMessages"] == [{"role": "user", "content": "flat"}]


def test_resolve_resume_empty_returns_none(monkeypatch):
    """No session found: returns (None, None)."""
    monkeypatch.setattr(_cli_mod, "get_latest_session_id", lambda: None)

    adopt_sid, data = _cli_mod._resolve_resume_session()
    assert adopt_sid is None
    assert data is None
