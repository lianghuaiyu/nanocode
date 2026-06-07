from nanocode.session import save_session, load_session, get_latest_session_id


def test_session_roundtrip():
    save_session("s1", {"metadata": {"id": "s1", "startTime": "2026-01-01"},
                        "anthropicMessages": [{"role": "user", "content": "hi"}]})
    data = load_session("s1")
    assert data["anthropicMessages"][0]["content"] == "hi"
    assert get_latest_session_id() == "s1"


def test_latest_picks_newest():
    save_session("a", {"metadata": {"id": "a", "startTime": "2026-01-01"}})
    save_session("b", {"metadata": {"id": "b", "startTime": "2026-02-01"}})
    assert get_latest_session_id() == "b"
