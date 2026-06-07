"""trace 开关与默认输出（项目本地 ./.nanocode/traces/）。"""
from __future__ import annotations

import os
from pathlib import Path

from .sinks import JsonlSink

_TRUE = {"1", "true", "yes", "on"}


def is_enabled(flag: bool = False) -> bool:
    if flag:
        return True
    return os.environ.get("NANOCODE_TRACE", "").strip().lower() in _TRUE


def trace_dir() -> Path:
    override = os.environ.get("NANOCODE_TRACE_DIR", "").strip()
    d = Path(override) if override else (Path.cwd() / ".nanocode" / "traces")
    d.mkdir(parents=True, exist_ok=True)
    return d


def trace_file(session_id: str) -> Path:
    return trace_dir() / f"{session_id}.jsonl"


def build_default_sinks(session_id: str) -> list:
    return [JsonlSink(trace_file(session_id))]
