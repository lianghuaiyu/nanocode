from nanocode.session import v2


def test_state_roundtrip():
    assert v2.read_state("s1") is None
    assert v2.is_v2_session("s1") is False
    v2.write_state("s1", {"startTime": "2026-06-06T00:00:00Z", "tasks": []})
    assert v2.is_v2_session("s1") is True
    assert v2.read_state("s1")["startTime"] == "2026-06-06T00:00:00Z"


def test_main_messages_roundtrip():
    v2.write_main_messages("s2", [{"role": "user", "content": "hi"}])
    assert v2.read_main_messages("s2") == [{"role": "user", "content": "hi"}]
    assert v2.read_main_messages("missing") == []


def test_agent_messages_roundtrip():
    v2.write_agent_messages("s3", "agent-001", [{"role": "user", "content": "x"}])
    assert v2.read_agent_messages("s3", "agent-001")[0]["content"] == "x"


def test_task_dir_created_under_session():
    d = v2.task_dir("s4", "task-001")
    assert d.is_dir() and d.name == "task-001" and "s4" in str(d)


def test_load_session_old_flat_json_unchanged():
    from nanocode.session import store
    store.save_session("old1", {"metadata": {"id": "old1", "startTime": "2026-06-01T00:00:00Z"},
                                "anthropicMessages": [{"role": "user", "content": "hi"}]})
    data = store.load_session("old1")
    assert data["anthropicMessages"][0]["content"] == "hi"
    assert data.get("v2") is not True


def test_load_session_v2_directory():
    from nanocode.session import store, v2
    v2.write_state("newd", {"id": "newd", "startTime": "2026-06-06T00:00:00Z", "tasks": [], "subagents": []})
    v2.write_main_messages("newd", [{"role": "user", "content": "yo"}])
    data = store.load_session("newd")
    assert data["v2"] is True
    assert data["anthropicMessages"][0]["content"] == "yo"
    assert data["state"]["id"] == "newd"


def test_get_latest_sees_both():
    from nanocode.session import store, v2
    store.save_session("oldx", {"metadata": {"id": "oldx", "startTime": "2026-06-01T00:00:00Z"}})
    v2.write_state("newx", {"id": "newx", "startTime": "2026-06-09T00:00:00Z"})
    assert store.get_latest_session_id() == "newx"
