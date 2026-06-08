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
            # torn-tail 安全：若已存在文件的最后一字节不是换行（上轮崩溃留下半行 JSON），
            # 先补一个换行，使残缺行独占一行（读侧 json.loads 会跳过它），新记录从干净行
            # 开始——否则新事件会被直接粘在半行后形成不可解析的合并行，导致 resume 后首个
            # 事件丢失、后续 parent_id 悬空。
            need_newline = False
            try:
                if self._path.exists() and self._path.stat().st_size > 0:
                    with self._path.open("rb") as r:
                        r.seek(-1, 2)
                        need_newline = r.read(1) != b"\n"
            except Exception:
                need_newline = False
            self._fh = self._path.open("a", encoding="utf-8")
            if need_newline:
                self._fh.write("\n")

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
