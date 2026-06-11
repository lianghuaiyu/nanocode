"""nanocode runtime 包（docs/15 §4/§11/§12）—— thread、child session、多 agent 协作编排层（L4）。

当前内容：
  teams.py   TeamRuntime 骨架（§12：TeamSession/TeamTaskBoard/AgentMailbox/ClaimLock/
             SharedArtifactStore/TeamEventStream）+ 预留 session entry 类型。

后续（Phase 6/7）：spawn.py（AgentRuntime.spawn_child）、thread.py/runtime.py（从 agent/runtime.py
迁入的 RuntimeThread/AgentRuntime）、approvals.py、events.py。
"""

from .teams import (
    TeamRuntime,
    TeamSession,
    TeamTaskBoard,
    TeamTask,
    ClaimLock,
    AgentMailbox,
    MailboxMessage,
    SharedArtifactStore,
    TeamEventStream,
)

__all__ = [
    "TeamRuntime",
    "TeamSession",
    "TeamTaskBoard",
    "TeamTask",
    "ClaimLock",
    "AgentMailbox",
    "MailboxMessage",
    "SharedArtifactStore",
    "TeamEventStream",
]
