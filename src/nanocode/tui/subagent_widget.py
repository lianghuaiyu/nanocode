"""Sub-agent run widget rendering.

This module is TUI-only: it consumes plain dictionaries returned by
RuntimeThread and never reads sessions or run sidecars directly.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from rich.console import Group
from rich.text import Text

from .selector import truncate_cells

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
MAX_WIDGET_LINES = 12
FINISHED_LINGER_SECONDS = 45
TERMINAL_STATUSES = {"completed", "failed", "blocked", "cancelled", "lost", "timed_out"}


def render_subagent_widget(records: list[dict[str, Any]], *, width: int, frame: int) -> Group | None:
    visible = _visible_records(records)
    if not visible:
        return None

    active = any(r["status"] in {"running", "queued"} for r in visible)
    lines: list[Text] = []
    heading = Text("● Agents" if active else "○ Agents", style="accent" if active else "dim")
    lines.append(_fit_text(heading, width))

    body_budget = max(1, MAX_WIDGET_LINES - 1)
    body: list[tuple[str, list[Text]]] = []
    for rec in visible:
        body.append((rec["status"], _record_lines(rec, width=width, frame=frame)))

    flattened: list[Text] = []
    hidden = 0
    for _status, rec_lines in body:
        if len(flattened) + len(rec_lines) <= body_budget:
            flattened.extend(rec_lines)
        else:
            hidden += 1
    if hidden:
        if len(flattened) >= body_budget:
            flattened = flattened[:body_budget - 1]
        flattened.append(Text(f"└─ +{hidden} more", style="dim"))
    _fix_last_connector(flattened)
    lines.extend(flattened)
    return Group(*lines)


def _visible_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    active: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    for rec in records:
        status = rec["status"]
        if status in {"running", "queued"}:
            active.append(rec)
        elif status in TERMINAL_STATUSES and _recent_enough(rec, now):
            finished.append(rec)
    active.sort(key=lambda r: r.get("started_at") or r.get("created_at") or "")
    finished.sort(key=lambda r: r.get("ended_at") or "", reverse=True)
    return active + finished


def _recent_enough(rec: dict[str, Any], now: float) -> bool:
    ended = _epoch(rec.get("ended_at"))
    return ended is not None and now - ended <= FINISHED_LINGER_SECONDS


def _record_lines(rec: dict[str, Any], *, width: int, frame: int) -> list[Text]:
    status = rec["status"]
    prefix = "├─"
    desc = str(rec["description"]).strip()
    agent_type = str(rec["agent_type"])
    metrics = rec.get("metrics") if isinstance(rec.get("metrics"), dict) else {}
    stats = _stats(rec, metrics)
    label = agent_type[0].upper() + agent_type[1:]

    if status == "running":
        icon = SPINNER[frame % len(SPINNER)]
        line = Text(f"{prefix} {icon} ", style="dim")
        line.append(label, style="bold")
        if desc:
            line.append(f"  {desc}", style="muted")
        if stats:
            line.append(" · " + stats, style="dim")
        activity = _activity(metrics)
        return [
            _fit_text(line, width),
            _fit_text(Text(f"│    ⎿ {activity}", style="dim"), width),
        ]
    if status == "queued":
        line = Text(f"{prefix} ◦ {label}", style="dim")
        if desc:
            line.append(f"  {desc}", style="muted")
        if stats:
            line.append(" · " + stats, style="dim")
        return [_fit_text(line, width)]

    icon = "✓" if status == "completed" else "✗"
    line = Text(f"{prefix} {icon} {label}", style="success" if status == "completed" else "warning")
    if desc:
        line.append(f"  {desc}", style="muted")
    if stats:
        line.append(" · " + stats, style="dim")
    if status and status != "completed":
        line.append(f" · {status}", style="warning")
    return [_fit_text(line, width)]


def _stats(rec: dict[str, Any], metrics: dict[str, Any]) -> str:
    parts: list[str] = []
    turns = int(metrics.get("turnCount") or 0)
    if turns:
        parts.append(f"↻{turns}")
    tools = int(metrics.get("toolUses") or 0)
    if tools:
        parts.append(f"{tools} tool use{'' if tools == 1 else 's'}")
    tokens = _token_count(metrics.get("usage"))
    if tokens:
        parts.append(_format_tokens(tokens))
    duration = _duration(rec)
    if duration:
        parts.append(duration)
    return " · ".join(parts)


def _token_count(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("input") or 0) + int(usage.get("output") or 0)


def _format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M token"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k token"
    return f"{count} token"


def _duration(rec: dict[str, Any]) -> str:
    start = _epoch(rec.get("started_at") or rec.get("created_at"))
    if start is None:
        return ""
    end = _epoch(rec.get("ended_at")) or time.time()
    seconds = max(0, int(end - start))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h"


def _activity(metrics: dict[str, Any]) -> str:
    active = metrics.get("activeTools") or []
    current = active[-1] if isinstance(active, list) and active else {}
    if isinstance(current, dict):
        tool = current.get("tool") or metrics.get("currentTool")
        detail = current.get("inputSummary") or ""
    else:
        tool = metrics.get("currentTool")
        detail = ""
    verb = {
        "read_file": "reading",
        "list_files": "listing",
        "search_files": "searching",
        "run_shell": "running command",
        "edit_file": "editing",
        "write_file": "writing",
    }.get(str(tool or ""), str(tool or "thinking"))
    detail = " ".join(str(detail).split())
    if detail:
        return f"{verb} {detail}"
    return verb if verb != "thinking" else "thinking..."


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def _fit_text(text: Text, width: int) -> Text:
    plain = truncate_cells(text.plain, max(1, width))
    if plain == text.plain:
        return text
    return Text(plain, style=text.style)


def _fix_last_connector(lines: list[Text]) -> None:
    if not lines:
        return
    last = lines[-1]
    if last.plain.startswith("│"):
        lines[-1] = Text("   " + last.plain[3:], style=last.style)
        if len(lines) >= 2:
            prev = lines[-2]
            if prev.plain.startswith("├─"):
                lines[-2] = Text("└─" + prev.plain[2:], style=prev.style)
        return
    if last.plain.startswith("├─"):
        lines[-1] = Text("└─" + last.plain[2:], style=last.style)
