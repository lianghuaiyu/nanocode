"""Serializable run-record projection models.

These models describe the child-owned ``subagent-run/`` sidecar. They are not
session identity or transcript truth; that remains ``session.jsonl``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


TERMINAL_RUN_STATUSES = frozenset({
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "lost",
    "timed_out",
})


@dataclass
class RunMetrics:
    tool_uses: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    turn_count: int = 0
    compaction_count: int = 0
    active_tools: list[dict[str, Any]] = field(default_factory=list)
    current_tool: str | None = None
    current_tool_started_at: str | None = None
    last_event_at: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "RunMetrics":
        if not isinstance(value, dict):
            return cls()
        return cls(
            tool_uses=int(value.get("toolUses") or value.get("tool_uses") or 0),
            usage=dict(value.get("usage") or {}),
            turn_count=int(value.get("turnCount") or value.get("turn_count") or 0),
            compaction_count=int(value.get("compactionCount") or value.get("compaction_count") or 0),
            active_tools=list(value.get("activeTools") or value.get("active_tools") or []),
            current_tool=value.get("currentTool"),
            current_tool_started_at=value.get("currentToolStartedAt"),
            last_event_at=value.get("lastEventAt") or value.get("last_event_at"),
        )

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "toolUses": self.tool_uses,
            "usage": dict(self.usage),
            "turnCount": self.turn_count,
            "compactionCount": self.compaction_count,
            "activeTools": list(self.active_tools),
            "currentTool": self.current_tool,
            "currentToolStartedAt": self.current_tool_started_at,
            "lastEventAt": self.last_event_at,
        }


@dataclass
class AgentRunRecord:
    run_id: str
    child_session_id: str
    parent_session_id: str
    status: str
    agent_type: str
    model: dict[str, Any] | None = None
    background: bool = False
    context_mode: str = "fresh"
    isolation: str = "shared"
    worktree_path: str | None = None
    metrics: RunMetrics = field(default_factory=RunMetrics)
    result_path: str | None = None
    result_entry_id: str | None = None
    prompt_entry_id: str | None = None
    spawn_entry_id: str | None = None
    tool_call_id: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    pending_steer_count: int = 0
    summary: str | None = None

    @classmethod
    def from_status(cls, status: dict[str, Any], *, summary: str | None = None) -> "AgentRunRecord":
        return cls(
            run_id=status["runId"],
            child_session_id=status["childSessionId"],
            parent_session_id=status["parentSessionId"],
            status=status["status"],
            agent_type=status.get("agentType") or "coder",
            model=status.get("model"),
            background=bool(status.get("background")),
            context_mode=status.get("contextMode") or "fresh",
            isolation=status.get("isolation") or "shared",
            worktree_path=status.get("worktreePath"),
            metrics=RunMetrics.from_dict(status.get("metrics")),
            result_path=status.get("resultPath"),
            result_entry_id=status.get("resultEntryId"),
            prompt_entry_id=status.get("promptEntryId"),
            spawn_entry_id=status.get("spawnEntryId"),
            tool_call_id=status.get("toolCallId"),
            created_at=status.get("createdAt"),
            started_at=status.get("startedAt"),
            ended_at=status.get("endedAt"),
            error=status.get("error"),
            pending_steer_count=int(status.get("pendingSteerCount") or 0),
            summary=summary,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["metrics"] = self.metrics.to_status_dict()
        return data


@dataclass
class RunEvent:
    type: str
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "timestamp": self.timestamp, **self.data}
