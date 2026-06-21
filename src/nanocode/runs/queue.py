"""FIFO run queue placeholder.

The durable queue state is represented by run-record statuses. This module is a
small in-memory limiter helper; it deliberately does not persist independent
truth.
"""

from __future__ import annotations

from collections import deque


class RunQueue:
    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self._running: set[str] = set()
        self._queued: deque[str] = deque()

    def submit(self, run_id: str) -> str:
        if self.limit <= 0 or len(self._running) < self.limit:
            self._running.add(run_id)
            return "running"
        self._queued.append(run_id)
        return "queued"

    def finish(self, run_id: str) -> str | None:
        self._running.discard(run_id)
        if not self._queued:
            return None
        nxt = self._queued.popleft()
        self._running.add(nxt)
        return nxt
