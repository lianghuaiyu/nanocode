"""session/lease.py — SessionLease：runtime active-thread 持有的「会话写者租约」（docs/14）。

设计来自 Pi + Codex 的混合：
  - Pi：`session.jsonl` 树是唯一上下文权威；
  - Codex：runtime 的 active thread 持有 live writer；
  - nanocode：active writer 额外持一把跨进程 `fcntl.flock`，其生命周期 = 这个 lease。

关键：**写者身份不在 `Agent.__init__`**。构造一个模型 core 不取锁、不碰 session 文件、不决定
谁是唯一 writer。runtime（AgentRuntime 的 thread 生命周期）创建/取得 lease，把 lease 持有的
**已加锁** SessionManager 注入给 agent（`agent._session_mgr = lease.manager`）。写树时缺 manager
即 fatal——没有 flat fallback。

lease 由 RuntimeThread 持有；rebind/子 agent 完成/REPL 退出/一次性结束时 `close()` 释放锁。
"""

from __future__ import annotations

from .manager import SessionManager
from .tree import SessionTreeError


class SessionLease:
    """一把会话写者租约：封装一个**已加锁**的 SessionManager。

    构造即校验持锁（防止误用未加锁 mgr 当 writer）。`close()` 释放底层 flock（幂等）。
    不在此 `build_context()`——校验/渲染由调用方按需做（rebind 校验目标可折叠；新空 session 无可验）。
    """

    def __init__(self, mgr: SessionManager) -> None:
        if not mgr.locked:
            raise SessionTreeError("writer lease requires a locked SessionManager (lock=True)")
        self.manager = mgr

    @property
    def session_id(self) -> str:
        return self.manager.session_id

    @classmethod
    def open_or_create(cls, session_id: str, *, spawned_by: dict | None = None,
                       forked_from: dict | None = None, cwd: str | None = None) -> "SessionLease":
        """取得一个写者 lease：已存在树 → open(lock=True)；否则 create(lock=True) 写 root 并持锁。

        spawned_by/forked_from：docs/26 C2 血缘正交两键（fresh create 时写 header；
        subagent 传 spawned_by、fork 传 forked_from，各自至多其一）。
        目标被其它进程占用 → `SessionBusyError`；树损坏（非末行 torn）→ `SessionTreeError`。
        二者都由调用方处理（fail-closed：startup 退出 / `/resume` 提示 `--fork`）。"""
        mgr = (SessionManager.open(session_id, lock=True) if SessionManager.exists(session_id)
               else SessionManager.create(session_id, cwd=cwd, spawned_by=spawned_by,
                                          forked_from=forked_from, lock=True,
                                          defer_persist=True))
        return cls(mgr)

    def close(self) -> None:
        """释放写锁（幂等）。rebind 交接旧 lease、子 agent 完成、REPL/一次性退出时调用。"""
        self.manager.close()
