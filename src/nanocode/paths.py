"""集中管理 nanocode 的本地存储根（默认 ~/.nanocode，可用 NANOCODE_HOME 覆盖）。"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("NANOCODE_HOME")
    return Path(base) if base else Path.home() / ".nanocode"


def sessions_dir() -> Path:
    d = data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tool_results_dir() -> Path:
    d = data_dir() / "tool-results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_memory_dir() -> Path:
    h = hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]
    d = data_dir() / "projects" / h / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def trust_file() -> Path:
    """工作区信任存储文件：data_dir()/trust.json（绝不放进项目 .claude/）。"""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "trust.json"


def history_file() -> Path:
    """REPL 行编辑历史文件：data_dir()/history。"""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "history"
