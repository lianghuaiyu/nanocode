"""trace 开关与默认输出（项目本地 ./.nanocode/traces/）。"""
from __future__ import annotations

import os
from pathlib import Path

from .sinks import JsonlSink

_TRUE = {"1", "true", "yes", "on"}
_VALID_TRAJECTORY_LEVELS = {"summary", "full"}


def is_enabled(flag: bool = False) -> bool:
    if flag:
        return True
    return os.environ.get("NANOCODE_TRACE", "").strip().lower() in _TRUE


def trajectory_enabled(flag: bool = False) -> bool:
    """trajectory 采集是否开启：显式 flag 优先，否则看 NANOCODE_TRAJECTORY 环境变量。"""
    if flag:
        return True
    return os.environ.get("NANOCODE_TRAJECTORY", "").strip().lower() in _TRUE


def trajectory_level(value: "str | None" = None) -> str:
    """trajectory 采集级别：显式 value 优先，否则看 NANOCODE_TRAJECTORY_LEVEL 环境变量；
    非法/缺省 → "summary"（保守默认，不写完整 payload）。"""
    raw = (value or os.environ.get("NANOCODE_TRAJECTORY_LEVEL", "")).strip().lower()
    return raw if raw in _VALID_TRAJECTORY_LEVELS else "summary"


def trace_dir() -> Path:
    override = os.environ.get("NANOCODE_TRACE_DIR", "").strip()
    d = Path(override) if override else (Path.cwd() / ".nanocode" / "traces")
    d.mkdir(parents=True, exist_ok=True)
    return d


def trace_file(session_id: str) -> Path:
    return trace_dir() / f"{session_id}.jsonl"


def build_default_sinks(session_id: str) -> list:
    return [JsonlSink(trace_file(session_id))]
