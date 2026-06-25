"""memory/jobs.py — generation job lease (docs/20 §7 Phase 6).

A flock-based, non-blocking lease so two memory-generation workers can never run
concurrently against the same store. The lease file lives under the engine's
own scoped root; acquisition is best-effort and never blocks.
"""
from __future__ import annotations

import os
from pathlib import Path


class MemoryJobLease:
    """Non-reentrant, non-blocking advisory lease over a store root."""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd: "int | None" = fd
        self.path = path

    @classmethod
    def acquire(cls, root: str, *, name: str = "generation",
                timeout: float = 0.0, poll: float = 0.05) -> "MemoryJobLease | None":
        """Try to take the lease. Returns None if another worker holds it.

        `timeout=0` (default) is a single non-blocking attempt. `timeout>0` retries
        the non-blocking flock every `poll` seconds until acquired or the deadline
        passes — a *bounded* best-effort wait so a near-simultaneous teardown race
        resolves in the common case (the winner finishes fast) instead of dropping
        the loser's generation run (docs/21 §13.2 / D5). It never blocks unboundedly,
        and a lost race after the deadline is still "not run", never "generated"."""
        import fcntl
        import time
        rp = Path(root)
        rp.mkdir(parents=True, exist_ok=True)
        path = rp / f".{name}.lock"
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return cls(fd, path)
            except OSError:
                os.close(fd)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # clamp so we never overshoot the deadline; max(0.0, …) guards a
                # non-positive poll from raising on time.sleep.
                time.sleep(max(0.0, min(poll, remaining)))

    def release(self) -> None:
        if self._fd is not None:
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "MemoryJobLease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
