"""后台任务 / 子 agent 的数据模型（纯数据，可序列化进 session v2 state.json）。"""
from __future__ import annotations

from dataclasses import dataclass, asdict, fields

TASK_KINDS = ("subagent", "shell", "memory_consolidate", "memory_eval", "memory_optimize")
TASK_STATUSES = ("running", "completed", "failed", "blocked", "cancelled", "lost", "timed_out")
TERMINAL_TASK_STATUSES = ("completed", "failed", "blocked", "cancelled", "lost", "timed_out")
SUBAGENT_STATUSES = ("idle", "running", "completed", "failed", "blocked", "cancelled", "lost", "timed_out")


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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskRecord":
        keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in keys})


@dataclass
class SubAgentRecord:
    id: str
    type: str = "coder"
    description: str = ""
    status: str = "idle"
    model: str | None = None
    provider: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    message_path: str | None = None
    last_result_path: str | None = None
    task_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubAgentRecord":
        keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in keys})
