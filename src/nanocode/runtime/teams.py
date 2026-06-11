"""runtime/teams.py — TeamRuntime 骨架（docs/15 §12）。

subagent ≠ team：subagent 是 parent 委派的 child（child 向 parent 汇报、隔离上下文）；team 是
**共享任务板 + agent 互相通信**的对等协作。§12 明确：**不要**把多 agent 协作塞进 `agent` 工具,
预留独立 runtime。本文件是骨架——可创建 team session + task board + claim lock（可用的最小实现）,
mailbox/artifact/event 给出接口与内存实现;真正的协作调度（spawn 对等 agent、跨 agent 消息路由）
留待后续实现,但状态模型已预留。

不变量（§12 acceptance）：
- 能创建带 task board 的 team session;
- claim lock 保证一个任务同时只被一个 agent 认领（无双认领）;
- agent-to-agent 通信走 AgentMailbox / team session entry,**绝不**进父对话 transcript（不 overload
  task_update / 父 tool result）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 预留的 team session entry 类型（schema 已在 session/tree.py 保留;此处 re-export 供 runtime 使用）。
from ..session.tree import (
    AGENT_MAILBOX_MESSAGE, TEAM_CLAIM, TEAM_MESSAGE, TEAM_RESULT, TEAM_START, TEAM_TASK_UPDATE,
)

TEAM_TASK_STATUSES = ("open", "claimed", "in_progress", "done", "failed")


@dataclass
class TeamTask:
    """team task board 上的一条任务。owner=None 表示未认领（open）。"""

    id: str
    description: str
    status: str = "open"
    owner: str | None = None
    result: str | None = None


class ClaimLock:
    """单认领锁：一个 task 同时只能被一个 agent 认领（§12 ClaimLock）。内存实现,单进程。"""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def claim(self, task_id: str, agent_id: str) -> bool:
        """认领 task。已被他人认领 → False（fail-closed,不抢占）;已被自己认领 → True（幂等）。"""
        cur = self._owners.get(task_id)
        if cur is not None and cur != agent_id:
            return False
        self._owners[task_id] = agent_id
        return True

    def owner(self, task_id: str) -> str | None:
        return self._owners.get(task_id)

    def release(self, task_id: str, agent_id: str) -> bool:
        if self._owners.get(task_id) == agent_id:
            del self._owners[task_id]
            return True
        return False


class TeamTaskBoard:
    """共享任务板（§12 TeamTaskBoard）。add/claim/update/list;认领经 ClaimLock 串行化。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TeamTask] = {}
        self._lock = ClaimLock()
        self._seq = 0

    def add(self, description: str, *, task_id: str | None = None) -> TeamTask:
        if task_id is None:
            self._seq += 1
            task_id = f"tt{self._seq}"
        t = TeamTask(id=task_id, description=description)
        self._tasks[task_id] = t
        return t

    def claim(self, task_id: str, agent_id: str) -> bool:
        """认领一个 open task。成功 → 标 claimed + owner;任务不存在或已被他人认领 → False。"""
        if task_id not in self._tasks:
            return False
        if not self._lock.claim(task_id, agent_id):
            return False
        t = self._tasks[task_id]
        t.owner = agent_id
        if t.status == "open":
            t.status = "claimed"
        return True

    def update(self, task_id: str, *, status: str | None = None, result: str | None = None) -> bool:
        t = self._tasks.get(task_id)
        if t is None:
            return False
        if status is not None:
            t.status = status
        if result is not None:
            t.result = result
        return True

    def get(self, task_id: str) -> "TeamTask | None":
        return self._tasks.get(task_id)

    def list(self, *, status: str | None = None) -> list[TeamTask]:
        return [t for t in self._tasks.values() if status is None or t.status == status]


@dataclass
class MailboxMessage:
    """agent-to-agent 消息（§12 AgentMailbox）。绝不进父对话 transcript。"""

    sender: str
    recipient: str
    body: str


class AgentMailbox:
    """对等 agent 间的消息箱（§12）。内存实现;send/inbox。"""

    def __init__(self) -> None:
        self._inboxes: dict[str, list[MailboxMessage]] = {}

    def send(self, sender: str, recipient: str, body: str) -> MailboxMessage:
        msg = MailboxMessage(sender=sender, recipient=recipient, body=body)
        self._inboxes.setdefault(recipient, []).append(msg)
        return msg

    def inbox(self, agent_id: str) -> list[MailboxMessage]:
        return list(self._inboxes.get(agent_id, []))

    def drain(self, agent_id: str) -> list[MailboxMessage]:
        msgs = self._inboxes.get(agent_id, [])
        self._inboxes[agent_id] = []
        return msgs


class SharedArtifactStore:
    """team 共享产物（§12）。内存键值;put/get/keys。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self._store[key] = value

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def keys(self) -> list[str]:
        return list(self._store)


class TeamEventStream:
    """team 事件流（§12）。内存追加;后续可投影到 team session.jsonl 的 team_* entry。"""

    def __init__(self) -> None:
        self._events: list[dict] = []

    def emit(self, kind: str, **fields) -> dict:
        ev = {"kind": kind, **fields}
        self._events.append(ev)
        return ev

    def events(self) -> list[dict]:
        return list(self._events)


@dataclass
class TeamSession:
    """一个 team 协作会话（§12 TeamSession）。聚合 board / mailbox / artifacts / events + 成员。"""

    team_id: str
    members: list[str] = field(default_factory=list)
    board: TeamTaskBoard = field(default_factory=TeamTaskBoard)
    mailbox: AgentMailbox = field(default_factory=AgentMailbox)
    artifacts: SharedArtifactStore = field(default_factory=SharedArtifactStore)
    events: TeamEventStream = field(default_factory=TeamEventStream)

    def add_member(self, agent_id: str) -> None:
        if agent_id not in self.members:
            self.members.append(agent_id)


class TeamRuntime:
    """多 agent 协作编排层骨架（§12）。当前可创建/取得 team session;真正的对等 spawn + 消息路由
    调度留待后续（状态模型已预留）。绝不在 `agent` 工具内做这些。"""

    def __init__(self) -> None:
        self._teams: dict[str, TeamSession] = {}
        self._seq = 0

    def create_team(self, members: "list[str] | None" = None, *, team_id: str | None = None) -> TeamSession:
        if team_id is None:
            self._seq += 1
            team_id = f"team{self._seq}"
        ts = TeamSession(team_id=team_id, members=list(members or []))
        ts.events.emit(TEAM_START, team_id=team_id, members=ts.members)
        self._teams[team_id] = ts
        return ts

    def team(self, team_id: str) -> "TeamSession | None":
        return self._teams.get(team_id)

    def teams(self) -> list[TeamSession]:
        return list(self._teams.values())
