"""trace 输出后端（sink）协议与默认 JSONL 实现。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class Sink(Protocol):
    def write(self, event: dict) -> None: ...
    def close(self) -> None: ...


class JsonlSink:
    """把事件逐行追加为 JSON 写入文件。延迟建文件；任一写入失败后自禁用，绝不抛出。"""

    def __init__(self, path: "Path | str") -> None:
        self._path = Path(path)
        self._fh = None
        self._disabled = False

    def _ensure_open(self) -> None:
        if self._fh is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")

    def write(self, event: dict) -> None:
        if self._disabled:
            return
        try:
            self._ensure_open()
            self._fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        except Exception:
            self._disabled = True  # I/O 故障后不再尝试，绝不影响 agent

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
