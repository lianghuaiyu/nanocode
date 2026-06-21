"""Host background task registry. Serializable into session v2 state.json."""
from __future__ import annotations

import time

from .models import TASK_KINDS, TaskRecord, TERMINAL_TASK_STATUSES


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._task_seq = 0

    def create_task(self, kind: str, description: str = "", owner_agent_id: str | None = None,
                    id: str | None = None) -> TaskRecord:
        if kind not in TASK_KINDS:
            raise ValueError(f"unknown host task kind: {kind}")
        self._task_seq += 1
        t = TaskRecord(id=id or f"task-{self._task_seq:03d}", kind=kind, description=description,
                       owner_agent_id=owner_agent_id, started_at=_now())
        self._tasks[t.id] = t
        return t

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def list_tasks(self, status: str | None = None) -> list[TaskRecord]:
        return [t for t in self._tasks.values() if status is None or t.status == status]

    def update_task(self, task_id: str, **fields) -> TaskRecord | None:
        t = self._tasks.get(task_id)
        if not t:
            return None
        for k, v in fields.items():
            setattr(t, k, v)
        if fields.get("status") in TERMINAL_TASK_STATUSES and t.ended_at is None:
            t.ended_at = _now()
        return t

    def to_state(self) -> dict:
        return {
            "tasks": [t.to_dict() for t in self._tasks.values()],
            "task_seq": self._task_seq,
        }

    def load_state(self, state: dict) -> None:
        for d in state.get("tasks", []):
            t = TaskRecord.from_dict(d)
            if t.kind not in TASK_KINDS:
                continue
            self._tasks[t.id] = t
        self._task_seq = max([state.get("task_seq", 0)] + [self._seq(i, "task-") for i in self._tasks])

    @staticmethod
    def _seq(id_str: str, prefix: str) -> int:
        try:
            return int(id_str[len(prefix):])
        except (ValueError, TypeError):
            return 0
