from __future__ import annotations

import asyncio

from nanocode.tui.selector import Outcome
from nanocode.tui.session_pages.agents import (
    AgentRunsModel,
    AgentTypesModel,
    ConversationModel,
    TextViewerModel,
    run_agent_runs,
    view_agent_definitions,
)


def _record() -> dict:
    return {
        "status": "completed",
        "agent_type": "explore",
        "description": "agent smoke two",
        "child_session_id": "sess_child_12345678",
        "metrics": {"usage": {"input": 12, "output": 3}},
        "started_at": "2026-06-29T13:14:10Z",
        "ended_at": "2026-06-29T13:14:12Z",
        "group_id": "orch_123",
    }


def test_conversation_model_header_renders_after_hint_init():
    model = ConversationModel({
        "record": _record(),
        "messages": [
            {"role": "user", "content": "Reply exactly OK."},
            {"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
        ],
    })

    header = "\n".join(model.header_lines(100))
    body = "\n".join(model.items())

    assert "sess_child_12345678" in header
    assert "Reply exactly OK." in body
    assert "OK" in body


def test_conversation_model_renders_run_error_in_body():
    record = _record()
    record["status"] = "failed"
    record["error"] = "Sub-agent left background shell task(s) still running: task-001."
    model = ConversationModel({
        "record": record,
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "CHILD_DONE"}]},
        ],
    })

    body = "\n".join(model.items())

    assert "[Error]" in body
    assert "task-001" in body


def test_conversation_model_running_cancel_requires_second_x():
    record = _record()
    record["status"] = "running"
    model = ConversationModel({"record": record, "messages": []})

    first = model.on_key("x", "", 0)
    second = model.on_key("x", "", 0)

    assert first is not None and first.kind == "refresh"
    assert second is not None and second.kind == "edit" and second.edit_action == "cancel"
    assert "x again to STOP" in "\n".join(model.header_lines(100))


def test_conversation_model_refreshes_snapshot_for_header_and_body():
    running = _record()
    running["status"] = "running"
    completed = _record()
    completed["status"] = "completed"

    def load_snapshot():
        return {
            "record": completed,
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "CHILD_DONE"}]},
            ],
        }

    model = ConversationModel(
        {"record": running, "messages": []},
        snapshot_loader=load_snapshot,
    )

    header = "\n".join(model.header_lines(100))
    body = "\n".join(model.items())

    assert "completed" in header
    assert "running" not in header
    assert "CHILD_DONE" in body


def test_agent_types_model_uses_agent_definitions_as_options():
    model = AgentTypesModel(
        "Available agent definitions:\n"
        "  explore  —  Read-only exploration\n"
        "  coder  —  Full coding agent\n"
    )

    items = model.items()
    rendered = model.list_text(items[1], True, 80)
    position = model.position_line(1, len(items), 0, len(items), 80)

    assert [item.name for item in items] == ["explore", "coder"]
    assert "› coder" in rendered
    assert position == "  (2/2)"


def test_view_agent_definitions_opens_selected_agent_detail():
    class Thread:
        def __init__(self) -> None:
            self.detail_calls: list[str] = []

        def agent_definitions(self):
            return (
                "Available agent definitions:\n"
                "  explore  —  Read-only exploration\n"
                "  coder  —  Full coding agent\n"
            )

        def agent_detail(self, name):
            self.detail_calls.append(name)
            return f"Agent definition: {name}\nEffective tools (1): run_shell"

    class Host:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run_selector(self, model, *, initial_index=None):
            self.calls.append(type(model).__name__)
            if isinstance(model, AgentTypesModel):
                if self.calls.count("AgentTypesModel") > 1:
                    return Outcome("cancel", index=1)
                item = model.items()[1]
                return Outcome("done", item=item, index=1)
            if isinstance(model, TextViewerModel):
                assert "Agent definition: coder" in "\n".join(model.items())
                return Outcome("cancel", index=0)
            raise AssertionError(type(model).__name__)

    thread = Thread()
    host = Host()

    asyncio.run(view_agent_definitions(thread, host=host))

    assert thread.detail_calls == ["coder"]
    assert host.calls == ["AgentTypesModel", "TextViewerModel", "AgentTypesModel"]


def test_run_agent_runs_enter_opens_conversation_view():
    class Thread:
        def subagent_widget_snapshot(self):
            return [_record()]

        def subagent_conversation_snapshot(self, child_session_id):
            assert child_session_id == "sess_child_12345678"
            return {
                "record": _record(),
                "messages": [
                    {"role": "user", "content": "Reply exactly OK."},
                    {"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
                ],
            }

    class Host:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run_selector(self, model, *, initial_index=None):
            self.calls.append(type(model).__name__)
            if isinstance(model, AgentRunsModel):
                if self.calls.count("AgentRunsModel") > 1:
                    return Outcome("cancel", index=0)
                item = model.items()[0]
                return Outcome("done", item=item, index=0)
            if isinstance(model, ConversationModel):
                model.header_lines(100)
                assert any("OK" in line for line in model.items())
                return Outcome("cancel", index=0)
            raise AssertionError(type(model).__name__)

    host = Host()

    result = asyncio.run(run_agent_runs(Thread(), host=host))

    assert result is None
    assert host.calls == ["AgentRunsModel", "ConversationModel", "AgentRunsModel"]
