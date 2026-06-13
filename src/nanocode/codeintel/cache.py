"""codeintel/cache.py — tags 磁盘缓存（diskcache 可选 extra；aider TAGS_CACHE 复刻）。

aider 用 diskcache(sqlite) 按文件 mtime 缓存每文件 tags（`.aider.tags.cache.vN/`）——冷启动
（新进程）免重解析，只对 mtime 变化的文件重抽取。本模块复刻该语义：

- 装了 diskcache（codeintel extra）→ 持久化到 root/.nanocode/<DIR>/；
- 没装 / 打开失败 → 内存 dict（进程内,等价旧行为,跨进程不持久）；
- sqlite 损坏（OperationalError/DatabaseError/OSError）→ 删目录重建 → 再失败回退内存
  （aider tags_cache_error 三段式）。

key = abs_path；val = {"mtime": float, "data": list[SymbolTag]}（SymbolTag 可 pickle）。
CACHE_VERSION 进目录名：SymbolTag schema 变更时 bump 即整体失效旧缓存。
"""

from __future__ import annotations

from pathlib import Path

CACHE_VERSION = 1
_CACHE_DIRNAME = f"codeintel.tags.cache.v{CACHE_VERSION}"

# diskcache 抛的可恢复错误（aider SQLITE_ERRORS 同款）
try:
    import sqlite3
    _SQLITE_ERRORS: tuple = (sqlite3.OperationalError, sqlite3.DatabaseError, OSError)
except Exception:                                   # pragma: no cover - sqlite3 是 stdlib
    _SQLITE_ERRORS = (OSError,)


def _open_diskcache(path: Path):
    """打开 diskcache.Cache；diskcache 未装 → None（调用方回退内存 dict）。"""
    try:
        from diskcache import Cache
    except Exception:
        return None
    try:
        return Cache(str(path))
    except _SQLITE_ERRORS:
        return None


class TagsCache:
    """每文件 tags 的 mtime 缓存。persistent=True 即 diskcache 落盘；否则进程内 dict。"""

    def __init__(self, root: str) -> None:
        self._dir = Path(root) / ".nanocode" / _CACHE_DIRNAME
        self._backend = _open_diskcache(self._dir)
        self.persistent = self._backend is not None
        if self._backend is None:
            self._backend = {}                      # 内存回退（dict-like 同接口）

    def get(self, abs_path: str, mtime: float):
        """mtime 命中 → 缓存的 tags；否则 None（miss/失效）。损坏 → 回退内存后再试一次。"""
        try:
            val = self._backend.get(abs_path)
        except _SQLITE_ERRORS as e:
            self._recover(e)
            val = self._backend.get(abs_path)
        if val is not None and val.get("mtime") == mtime:
            return val.get("data")
        return None

    def set(self, abs_path: str, mtime: float, data: list) -> None:
        try:
            self._backend[abs_path] = {"mtime": mtime, "data": data}
        except _SQLITE_ERRORS as e:
            self._recover(e)
            try:
                self._backend[abs_path] = {"mtime": mtime, "data": data}
            except _SQLITE_ERRORS:
                pass                                # 内存回退仍失败 → 放弃缓存（不影响正确性）

    def _recover(self, _err) -> None:
        """sqlite 损坏：删目录重建 diskcache；再失败 → 内存 dict（aider tags_cache_error）。"""
        if not self.persistent:
            return
        import shutil
        try:
            if self._dir.exists():
                shutil.rmtree(self._dir)
            new = _open_diskcache(self._dir)
            if new is not None:
                new["__probe__"] = 1               # 验活
                _ = new["__probe__"]
                del new["__probe__"]
                self._backend = new
                return
        except _SQLITE_ERRORS:
            pass
        self.persistent = False                     # 彻底回退内存
        self._backend = {}

    def close(self) -> None:
        if self.persistent:
            try:
                self._backend.close()
            except Exception:
                pass
