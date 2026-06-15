import sys

import pytest

from nanocode.agent import AgentRuntime
from nanocode.entrypoints import cli
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager, session_file


def test_parse_args_accepts_pi_session_flags(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "nanocode", "-c", "-n", "Named", "--no-session", "hello",
    ])
    args = cli.parse_args()
    assert args.continue_session is True
    assert args.session_name == "Named"
    assert args.no_session is True
    assert args.prompt == ["hello"]

    monkeypatch.setattr(sys, "argv", ["nanocode", "-r"])
    assert cli.parse_args().resume_picker is True

    monkeypatch.setattr(sys, "argv", ["nanocode", "--session", "abc", "--fork", "def"])
    args = cli.parse_args()
    assert args.session == "abc"
    assert args.fork_session == "def"


def test_parse_args_rejects_removed_resume_alias(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nanocode", "--resume"])

    with pytest.raises(SystemExit):
        cli.parse_args()


def test_resolve_session_arg_accepts_id_prefix_and_managed_path():
    mgr = SessionManager.create("abcdef12")
    mgr.close()

    sid, err = cli._resolve_session_arg("abc")
    assert err is None and sid == "abcdef12"

    sid, err = cli._resolve_session_arg(str(session_file("abcdef12")))
    assert err is None and sid == "abcdef12"


def test_runtime_startup_fork_clones_source_session():
    mgr = SessionManager.create("forksrc")
    mgr.append_message(T.user_message("source prompt"))
    mgr.close()

    child_sid, err = AgentRuntime().startup_fork_session("forksrc")

    assert err is None and child_sid and child_sid != "forksrc"
    child = SessionManager.open(child_sid)
    assert child.parent_session()["sessionId"] == "forksrc"
    assert "source prompt" in str(child.build_context().messages)


def test_no_session_cleanup_removes_generated_session_and_children():
    root = SessionManager.create("ephemroot")
    root.close()
    child = SessionManager.create("ephemchild", parent_session={"sessionId": "ephemroot"}, lock=False)

    assert session_file("ephemroot").exists()
    assert session_file("ephemchild").exists()

    cli._cleanup_ephemeral_session("ephemroot")

    assert not session_file("ephemroot").exists()
    assert not session_file("ephemchild").exists()
