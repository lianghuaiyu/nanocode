"""Tracer：在明确节点 emit 事件给各 sink；关闭态为零开销 no-op。"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .sinks import Sink

SCHEMA_VERSION = 1


class Tracer:
    def __init__(self, session_id: str, sinks: "list[Sink]", parent_session_id: "str | None" = None) -> None:
        self.session_id = session_id
        self.parent_session_id = parent_session_id
        self.sinks = sinks
        self._seq = 0

    def emit(self, type: str, **fields: Any) -> None:
        try:
            event = {
                "v": SCHEMA_VERSION,
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": self.session_id,
                "parent_session_id": self.parent_session_id,
                "seq": self._seq,
                "type": type,
                **fields,
            }
            self._seq += 1
        except Exception:
            return
        for sink in self.sinks:
            try:
                sink.write(event)
            except Exception:
                pass  # instrumentation 绝不影响 agent

    def child(self, session_id: str) -> "Tracer":
        return Tracer(session_id, self.sinks, parent_session_id=self.session_id)

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                pass


class NullTracer:
    """关闭态：全 no-op，零分配、零 I/O，不创建任何文件。"""

    session_id = ""
    parent_session_id = None

    def emit(self, *args: Any, **kwargs: Any) -> None:
        pass

    def child(self, *args: Any, **kwargs: Any) -> "NullTracer":
        return self

    def close(self) -> None:
        pass


def make_tracer(session_id: str, *, enabled: bool, sinks: "list[Sink] | None" = None):
    if not enabled:
        return NullTracer()
    if sinks is None:
        from .config import build_default_sinks
        sinks = build_default_sinks(session_id)
    parent = os.environ.get("NANOCODE_TRACE_PARENT", "").strip() or None
    return Tracer(session_id, sinks, parent_session_id=parent)
