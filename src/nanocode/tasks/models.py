"""Host background task data models.

Sub-agent runs are represented by child sessions plus ``subagent-run/`` records,
not by task records.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, fields

TASK_KINDS = ("shell", "memory_consolidate", "memory_eval", "memory_optimize")
TASK_STATUSES = ("running", "completed", "failed", "blocked", "cancelled", "lost", "timed_out")
TERMINAL_TASK_STATUSES = ("completed", "failed", "blocked", "cancelled", "lost", "timed_out")


@dataclass
class TaskRecord:
    id: str
    kind: str
    status: str = "running"
    description: str = ""
    owner_agent_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    result_path: str | None = None
    result_summary: str | None = None
    injected: bool = False
    error: str | None = None
    exit_code: int | None = None
    # docs/14 §6b：spawn 时父 session 的 leaf entry id。background 完成回注的 custom_message 据此
    # pin 到 spawn 分支（而非完成时的 live leaf）。随 state.json 持久（survives resume）。
    spawn_entry_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskRecord":
        keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in keys})
