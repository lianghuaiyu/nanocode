"""进程内后台任务 / 子 agent 注册表。可序列化进 session v2 state.json。"""
from __future__ import annotations

import time

from .models import TaskRecord, SubAgentRecord, TERMINAL_TASK_STATUSES


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._subagents: dict[str, SubAgentRecord] = {}
        self._task_seq = 0
        self._agent_seq = 0

    def create_task(self, kind: str, description: str = "", owner_agent_id: str | None = None) -> TaskRecord:
        self._task_seq += 1
        t = TaskRecord(id=f"task-{self._task_seq:03d}", kind=kind, description=description,
                       owner_agent_id=owner_agent_id, started_at=_now())
        self._tasks[t.id] = t
        return t

    def create_subagent(self, type: str = "coder", description: str = "",
                        model: str | None = None, provider: str | None = None) -> SubAgentRecord:
        self._agent_seq += 1
        a = SubAgentRecord(id=f"agent-{self._agent_seq:03d}", type=type, description=description,
                           model=model, provider=provider, created_at=_now(), updated_at=_now())
        self._subagents[a.id] = a
        return a

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def get_subagent(self, agent_id: str) -> SubAgentRecord | None:
        return self._subagents.get(agent_id)

    def list_tasks(self, status: str | None = None) -> list[TaskRecord]:
        return [t for t in self._tasks.values() if status is None or t.status == status]

    def list_subagents(self) -> list[SubAgentRecord]:
        return list(self._subagents.values())

    def update_task(self, task_id: str, **fields) -> TaskRecord | None:
        t = self._tasks.get(task_id)
        if not t:
            return None
        for k, v in fields.items():
            setattr(t, k, v)
        if fields.get("status") in TERMINAL_TASK_STATUSES and t.ended_at is None:
            t.ended_at = _now()
        return t

    def update_subagent(self, agent_id: str, **fields) -> SubAgentRecord | None:
        a = self._subagents.get(agent_id)
        if not a:
            return None
        for k, v in fields.items():
            setattr(a, k, v)
        a.updated_at = _now()
        return a

    def to_state(self) -> dict:
        return {
            "tasks": [t.to_dict() for t in self._tasks.values()],
            "subagents": [a.to_dict() for a in self._subagents.values()],
            "task_seq": self._task_seq,
            "agent_seq": self._agent_seq,
        }

    def load_state(self, state: dict) -> None:
        for d in state.get("tasks", []):
            t = TaskRecord.from_dict(d)
            self._tasks[t.id] = t
        for d in state.get("subagents", []):
            a = SubAgentRecord.from_dict(d)
            self._subagents[a.id] = a
        self._task_seq = max([state.get("task_seq", 0)] + [self._seq(i, "task-") for i in self._tasks])
        self._agent_seq = max([state.get("agent_seq", 0)] + [self._seq(i, "agent-") for i in self._subagents])

    @staticmethod
    def _seq(id_str: str, prefix: str) -> int:
        try:
            return int(id_str[len(prefix):])
        except (ValueError, TypeError):
            return 0
