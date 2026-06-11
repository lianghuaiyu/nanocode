"""P7 migration：legacy flat/v2 session → canonical session.jsonl 树（只 append、不删 legacy）。"""

from nanocode.session import tree
from nanocode.session.manager import SessionManager
from nanocode.session.migration import inspect_session, migrate_session
from nanocode.session.render import ModelCtx, render
from nanocode.session.store import save_session

LIVE = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": [{"type": "text", "text": "a"},
                                      {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "R"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
]


def test_migrate_flat_session_to_tree():
    save_session("mig1", {"metadata": {"id": "mig1"}, "anthropicMessages": LIVE, "openaiMessages": None})
    rep = migrate_session("mig1", model="claude-x")
    assert rep["status"] == "migrated" and rep["messages"] == 4
    # 树可重建出原 turn
    msgs = SessionManager.open("mig1").build_context().messages
    assert [m["role"] for m in msgs] == ["user", "assistant", "toolResult", "assistant"]
    payload = render(msgs, ModelCtx("anthropic", "anthropic", "claude-x"))["messages"]
    assert [m["role"] for m in payload] == ["user", "assistant", "user", "assistant"]


def test_migrate_idempotent_skips_existing_tree():
    save_session("mig2", {"metadata": {"id": "mig2"}, "anthropicMessages": LIVE})
    assert migrate_session("mig2")["status"] == "migrated"
    again = migrate_session("mig2")
    assert again["status"] == "skipped"


def test_migrate_not_found_and_empty():
    assert migrate_session("nope")["status"] == "not_found"
    save_session("mig3", {"metadata": {"id": "mig3"}, "anthropicMessages": []})
    assert migrate_session("mig3")["status"] == "empty"


def test_inspect_reports_tree_and_legacy():
    save_session("mig4", {"metadata": {"id": "mig4"}, "anthropicMessages": LIVE})
    rep = inspect_session("mig4")
    assert rep["legacy"]["exists"] and rep["legacy"]["messages"] == 4
    assert rep["tree"]["exists"] is False
    migrate_session("mig4")
    rep2 = inspect_session("mig4")
    assert rep2["tree"]["exists"] and rep2["tree"]["message_entries"] == 4


def test_sessions_cli_migrate_and_inspect():
    from nanocode.entrypoints.sessions_cmd import run
    save_session("cli1", {"metadata": {"id": "cli1"}, "anthropicMessages": LIVE})
    assert run(["migrate", "cli1"]) == 0
    assert SessionManager.exists("cli1")
    assert run(["inspect", "cli1"]) == 0
    assert run(["migrate", "all"]) == 0  # 幂等：已迁移的跳过
