"""Agents page for sub-agent run navigation and transcript viewing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..selector import ChoiceItem, ChoiceModel, KeyResult, Outcome, SelectorModel, truncate_cells
from ..theme import BOLD as _BOLD, DIM as _DIM, RESET as _RESET, fg as _fg

_ACCENT = _fg("accent")
_SUCCESS = _fg("success")
_WARN = _fg("warning")
_ERROR = _fg("error")


@dataclass(frozen=True)
class RunItem:
    record: dict[str, Any]


class AgentRunsModel(SelectorModel):
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = [RunItem(r) for r in records]

    def items(self) -> list[RunItem]:
        return self._records

    def border_accent(self) -> bool:
        return True

    def header_lines(self, width: int) -> list[str]:
        running = sum(1 for item in self._records if item.record["status"] in {"running", "queued"})
        done = len(self._records) - running
        return [
            f"{_BOLD}Sub-agent runs{_RESET}  {_DIM}{running} active · {done} done{_RESET}",
            f"{_DIM}Enter view · r resume child session · Esc back{_RESET}",
        ]

    def list_text(self, item: RunItem, selected: bool, width: int) -> str:
        rec = item.record
        status = str(rec["status"])
        icon = _status_icon(status)
        sid = str(rec["child_session_id"])
        agent = str(rec["agent_type"])
        desc = str(rec["description"]).strip()
        metrics = rec.get("metrics") if isinstance(rec.get("metrics"), dict) else {}
        stats = _stats(metrics)
        left = f"› {icon} {agent}" if selected else f"  {icon} {agent}"
        body = left + (f"  {desc}" if desc else "")
        tail = " · ".join(p for p in [status, stats, f"…{sid[-8:]}" if sid else ""] if p)
        if tail:
            body += f"  {tail}"
        return "  " + truncate_cells(body, max(1, width - 4))

    def empty_text(self, width: int) -> str:
        return "  No sub-agent runs in this session."

    def max_visible(self, height: int) -> int:
        return min(10, max(4, height - 8))

    def extra_keys(self) -> tuple[str, ...]:
        return ("r",)

    def on_key(self, key: str, item: RunItem, index: int) -> KeyResult | None:
        if key == "r" and item is not None:
            return KeyResult("edit", edit_action="resume")
        return None


class TextViewerModel(SelectorModel):
    def __init__(self, title: str, lines: list[str], *, hint: str = "Esc back") -> None:
        self._title = title
        self._lines = lines or ["(empty)"]
        self._hint = hint

    def items(self) -> list[str]:
        return self._lines

    def border_accent(self) -> bool:
        return True

    def header_lines(self, width: int) -> list[str]:
        return [f"{_BOLD}{self._title}{_RESET}", f"{_DIM}{self._hint}{_RESET}"]

    def list_text(self, item: str, selected: bool, width: int) -> str:
        return "  " + truncate_cells(item, max(1, width - 4))

    def position_line(self, index: int, total: int, visible_start: int, visible_end: int, width: int) -> str | None:
        if total <= 0:
            return None
        return f"  ({visible_start + 1}-{visible_end}/{total})"

    def max_visible(self, height: int) -> int:
        return max(6, height - 8)


class ConversationModel(TextViewerModel):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.record = snapshot["record"]
        self._stop_armed = False
        lines = _conversation_lines(snapshot.get("messages") or [])
        if not lines:
            lines = ["(waiting for first message...)"]
        super().__init__(_conversation_title(self.record), lines, hint=self._hint())

    def header_lines(self, width: int) -> list[str]:
        status = str(self.record["status"])
        metrics = self.record.get("metrics") if isinstance(self.record.get("metrics"), dict) else {}
        stats = _stats(metrics)
        desc = str(self.record["description"])
        first = f"{_BOLD}{_status_icon(status)} {self.record['agent_type']}{_RESET}"
        if desc:
            first += f"  {_DIM}{desc}{_RESET}"
        second = " · ".join(p for p in [status, stats, self.record["child_session_id"]] if p)
        return [first, f"{_DIM}{second}{_RESET}", f"{_DIM}{self._hint()}{_RESET}"]

    def extra_keys(self) -> tuple[str, ...]:
        return ("x", "r")

    def on_key(self, key: str, item: str, index: int) -> KeyResult | None:
        if key == "r":
            return KeyResult("edit", edit_action="resume")
        if key == "x" and self.record["status"] in {"running", "queued"}:
            if self._stop_armed:
                return KeyResult("edit", edit_action="cancel")
            self._stop_armed = True
            return KeyResult("refresh")
        return None

    def _hint(self) -> str:
        base = "↑↓ scroll · r resume · Esc back"
        if self.record["status"] in {"running", "queued"}:
            return ("x again to STOP · " if self._stop_armed else "x stop · ") + base
        return base


async def run_agents_page(thread, *, host) -> dict | None:
    index: int | None = None
    while True:
        records = _ordered_records(thread.subagent_widget_snapshot())
        choices: list[ChoiceItem] = []
        if records:
            running = sum(1 for r in records if r["status"] in {"running", "queued"})
            done = len(records) - running
            choices.append(ChoiceItem("Running agents", "runs", f"{running} active · {done} done"))
        choices.append(ChoiceItem("Agent types", "types", "available definitions"))
        outcome: Outcome = await host.run_selector(
            ChoiceModel("Agents", choices, hint="Enter select · Esc close"),
            initial_index=index,
        )
        index = outcome.index
        if outcome.kind == "cancel":
            return None
        if outcome.item.value == "types":
            await view_agent_text(host, "Agent types", thread.agent_definitions())
            continue
        result = await run_agent_runs(thread, host=host)
        if result is not None:
            return result


async def run_agent_runs(thread, *, host) -> dict | None:
    index: int | None = None
    while True:
        records = _ordered_records(thread.subagent_widget_snapshot())
        model = AgentRunsModel(records)
        outcome: Outcome = await host.run_selector(model, initial_index=index)
        index = outcome.index
        if outcome.kind == "cancel":
            return None
        if outcome.kind == "edit" and outcome.edit_action == "resume":
            return {"action": "resume", "session_id": _record_id(outcome.item.record)}
        if outcome.kind == "done" and outcome.item is not None:
            result = await view_agent_conversation(thread, _record_id(outcome.item.record), host=host)
            if result is not None:
                return result


async def view_agent_conversation(thread, child_session_id: str, *, host) -> dict | None:
    index: int | None = None
    while True:
        snapshot = thread.subagent_conversation_snapshot(child_session_id)
        outcome: Outcome = await host.run_selector(ConversationModel(snapshot), initial_index=index)
        index = outcome.index
        if outcome.kind in {"cancel", "done"}:
            return None
        if outcome.kind == "edit" and outcome.edit_action == "resume":
            return {"action": "resume", "session_id": child_session_id}
        if outcome.kind == "edit" and outcome.edit_action == "cancel":
            await thread.subagent_cancel(child_session_id)
            index = 0


async def view_agent_text(host, title: str, text: str) -> None:
    await host.run_selector(TextViewerModel(title, text.splitlines()))


def _ordered_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [r for r in records if r["status"] in {"running", "queued"}]
    finished = [r for r in records if r["status"] not in {"running", "queued"}]
    active.sort(key=lambda r: r.get("started_at") or r.get("created_at") or "")
    finished.sort(key=lambda r: r.get("ended_at") or r.get("started_at") or "", reverse=True)
    return active + finished


def _record_id(rec: dict[str, Any]) -> str:
    return str(rec["child_session_id"])


def _status_icon(status: str) -> str:
    if status == "completed":
        return f"{_SUCCESS}✓{_RESET}"
    if status in {"failed", "timed_out", "lost"}:
        return f"{_ERROR}✗{_RESET}"
    if status == "running":
        return f"{_ACCENT}●{_RESET}"
    if status == "queued":
        return f"{_WARN}◦{_RESET}"
    return f"{_DIM}○{_RESET}"


def _stats(metrics: dict[str, Any]) -> str:
    parts: list[str] = []
    turns = int(metrics.get("turnCount") or 0)
    tools = int(metrics.get("toolUses") or 0)
    usage = metrics.get("usage") if isinstance(metrics.get("usage"), dict) else {}
    tokens = int(usage.get("input") or 0) + int(usage.get("output") or 0)
    if turns:
        parts.append(f"↻{turns}")
    if tools:
        parts.append(f"{tools} tool use{'' if tools == 1 else 's'}")
    if tokens:
        parts.append(f"{tokens} token")
    return " · ".join(parts)


def _conversation_title(record: dict[str, Any]) -> str:
    return f"Sub-agent · {record['child_session_id']}"


def _conversation_lines(messages: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role")
        text, tools = _message_text_and_tools(msg.get("content"))
        if role == "user":
            if text.strip():
                _append_section(lines, "[User]", text)
        elif role == "assistant":
            if text.strip():
                _append_section(lines, "[Assistant]", text)
            for tool in tools:
                lines.append(f"  [Tool: {tool}]")
        elif role == "toolResult" and text.strip():
            _append_section(lines, "[Result]", _truncate_block(text, 500))
    return lines


def _append_section(lines: list[str], header: str, text: str) -> None:
    if lines:
        lines.append("───")
    lines.append(header)
    lines.extend(text.strip().splitlines())


def _message_text_and_tools(content) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "" if content is None else str(content), []
    text: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "text":
            text.append(block.get("text", ""))
        elif typ == "toolUse":
            tools.append(block.get("name") or block.get("toolName") or "unknown")
        elif typ == "toolResult":
            text.append(block.get("content", ""))
    return "\n".join(p for p in text if p), tools


def _truncate_block(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "... (truncated)"
