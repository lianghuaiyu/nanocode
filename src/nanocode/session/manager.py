"""session/manager.py — SessionManager：canonical `session.jsonl` 树存储（docs/13 §5/§6）。

对照 OpenCode `session-manager.ts`（sst@826419127，仅借 child-session 闭环、不借 SQLite/HTTP）。
一个 session 目录即完整事实源：`session.jsonl` append-only（可 `rewrite_file` GC），leaf 是日志
entry（无 state.json 权威，折叠重建）。torn-last-line 容忍（nanocode 特性，Pi 硬 throw——见 docs/13）。

P1：纯 session 层，不接 live agent（接线在 P2+）。I/O 在此；纯逻辑在 tree.py/context.py。
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import sessions_dir
from . import context, tree
from .context import ScalarState
from .tree import Entry


@dataclass
class BuiltContext:
    messages: list[dict]      # 中立 Message[]（user/assistant/toolResult），喂给 render.py
    scalar: ScalarState       # 折叠出的 model/thinking/activeTools


def session_root(session_id: str) -> Path:
    return sessions_dir() / session_id


def session_file(session_id: str) -> Path:
    return session_root(session_id) / "session.jsonl"


class SessionManager:
    """单一 session 的树存储 + 上下文构建。单写者（并发 lock 在 P5/§7.8，P1 不引入）。"""

    def __init__(self, session_id: str, entries: list[Entry]) -> None:
        self.session_id = session_id
        self._entries: list[Entry] = entries
        self._by_id: dict[str, Entry] = tree.index_by_id(entries)
        self._lock_fd = None   # 持有则为单写者（docs/14 §6a）；read-only 打开不持锁

    # ── 单写者锁（docs/14 §4.6/§6a）─────────────────────────────────────────────
    def acquire_lock(self) -> None:
        """对本 session 取 fcntl.flock(LOCK_EX|LOCK_NB) 独占写锁。已被占用 → SessionBusyError。
        进程死亡时 flock 由内核自动释放（advisory），无需手工 stale 检测。幂等。"""
        if self._lock_fd is not None:
            return
        path = session_root(self.session_id) / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fd.close()
            raise tree.SessionBusyError(f"session {self.session_id} is locked by another writer")
        self._lock_fd = fd

    def close(self) -> None:
        """释放写锁（若持有）。rebind/finalize 旧 session 时调用；幂等。"""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None

    @property
    def locked(self) -> bool:
        return self._lock_fd is not None

    def _require_writer(self) -> None:
        """所有 mutation（append/set_leaf/append_session_info/rewrite_file）的前置闸：未持写锁直接抛。

        docs/14 SessionLease：写者身份归 runtime 的 active-thread lease（lock=True 打开/创建），
        不再允许任何未加锁的 SessionManager 改树。read-only（open(lock=False) listing / build_context /
        entries）不受影响。_persist_new 是 create/clone 的 bootstrap 原语，**豁免**（create(lock=True)
        已在写 root 前持锁；clone 的 child 复制是交接前的 bootstrap，由后续 lease 接管）。"""
        if not self.locked:
            raise tree.SessionTreeError(
                f"session {self.session_id} mutated without a writer lease (lock=True required)")

    def __del__(self) -> None:
        # 安全网：mgr 被 GC 时释放 flock fd（避免持锁 SessionManager 泄漏 fd，尤其测试进程里
        # 大量短命 mgr）。close() 幂等且 guarded；解释器关停期属性可能已拆，故再包一层。
        try:
            self.close()
        except Exception:
            pass

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, session_id: str | None = None, *, cwd: str | None = None,
               parent_session: dict | None = None, lock: bool = True) -> "SessionManager":
        # docs/14 SessionLease：create 默认 lock=True——「新建一个 session」即写者意图，理应持锁。
        # 例外是 bootstrap-then-handoff：clone() 复制 child 后交给 runtime lease 重新加锁打开，故
        # 那里显式传 lock=False（避免同进程 clone 持锁 → rebind 重开 LOCK_NB 自锁死）。read-only
        # 打开走 open(lock=False)。
        sid = session_id or tree.new_id("sess")
        mgr = cls(sid, [])
        if lock:
            mgr.acquire_lock()      # 建前先占锁——并发同 sid 创建时第二者 fail-closed
        root = Entry(
            type=tree.SESSION_START,
            id=tree.new_id("ent"),
            parentId=None,
            sessionId=sid,
            timestamp=tree.now_iso(),
            data={"version": tree.V, "cwd": cwd or os.getcwd(),
                  **({"parentSession": parent_session} if parent_session else {})},
        )
        mgr._persist_new(root)
        return mgr

    @classmethod
    def open(cls, session_id: str, *, lock: bool = False) -> "SessionManager":
        path = session_file(session_id)
        entries: list[Entry] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    # torn last line 容忍：仅最后一行允许残缺（崩溃半写），否则抛。
                    if i == len(lines) - 1:
                        break
                    raise tree.SessionTreeError(f"corrupt session line {i + 1} in {path}")
                if isinstance(d, dict) and d.get("id"):
                    entries.append(Entry.from_dict(d))
        mgr = cls(session_id, entries)
        if lock:
            mgr.acquire_lock()      # 写者打开——已被占用则 SessionBusyError（read-only 打开不传 lock）
        return mgr

    @classmethod
    def exists(cls, session_id: str) -> bool:
        return session_file(session_id).exists()

    # ── 写入 ────────────────────────────────────────────────────────────────
    def _persist_new(self, entry: Entry) -> Entry:
        path = session_file(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False, default=str) + "\n")
        self._entries.append(entry)
        self._by_id[entry.id] = entry
        return entry

    def append(self, entry_type: str, data: dict | None = None, *, parent_id: str | None = None) -> Entry:
        """append 一条 entry（默认挂在当前 leaf 下）。返回该 entry。

        parent_id 显式给定时挂到指定 entry（语义父≠行序，供子会话回填等场景）。
        """
        self._require_writer()
        parent = parent_id if parent_id is not None else self.get_leaf()
        entry = Entry(type=entry_type, id=tree.new_id("ent"), parentId=parent,
                      sessionId=self.session_id, timestamp=tree.now_iso(), data=dict(data or {}))
        return self._persist_new(entry)

    def append_message(self, message: dict, *, parent_id: str | None = None) -> Entry:
        """append 一条中立 Message（user/assistant/toolResult）作 MESSAGE entry。"""
        return self.append(tree.MESSAGE, {"message": message}, parent_id=parent_id)

    def append_compaction(self, *, summary: str, tokens_before=None,
                          first_kept_entry_id: str | None = None, parent_id: str | None = None,
                          kind: str | None = None, message_count_before: int | None = None,
                          message_count_after: int | None = None) -> Entry:
        """S4（docs/13）：append 一条 compaction entry（summary + firstKeptEntryId），供
        build_context 两区 fold（context.py：摘要顶替被覆盖史 + 保留 firstKeptEntryId 起的消息）。

        kind / messageCountBefore / messageCountAfter（docs/14 Milestone B）：原只进 wire 的 compaction
        细节落树，供 trajectory 派生（eval 的 before/after 详情）；缺省 None（旧会话 fallback tokensBefore）。"""
        return self.append(tree.COMPACTION, {
            "summary": summary, "tokensBefore": tokens_before,
            "firstKeptEntryId": first_kept_entry_id,
            "kind": kind, "messageCountBefore": message_count_before,
            "messageCountAfter": message_count_after,
        }, parent_id=parent_id)

    def append_session_info(self, name: str) -> Entry:
        """Pi /name：写 session_info entry 设显示名（LWW，末个胜出、空则清空，见 tree.session_name）。
        session_info 不移动 leaf（leaf_id_after_entry 返回不变哨兵）。"""
        return self.append(tree.SESSION_INFO, {"name": name})

    def set_leaf(self, target_id: str | None) -> Entry:
        """移动 activeLeaf：append 一条 leaf entry（target_id=None → 复位到 root）。Pi setLeafId。"""
        if target_id is not None and target_id not in self._by_id:
            raise tree.SessionTreeError(f"set_leaf target {target_id} not found")
        return self.append(tree.LEAF, {"targetId": target_id})

    move_to = set_leaf  # in-file rewind/branch（docs/13 §7.2 /fork 默认 in-file）= 移 leaf

    # ── 读取 ────────────────────────────────────────────────────────────────
    def entries(self) -> list[Entry]:
        return list(self._entries)

    def get_leaf(self) -> str | None:
        return tree.current_leaf(self._entries)

    def get_branch(self, leaf_id: str | None = None) -> list[Entry]:
        leaf = leaf_id if leaf_id is not None else self.get_leaf()
        return tree.get_branch(self._by_id, leaf)

    def last_user_message_id(self, leaf_id: str | None = None) -> str | None:
        """branch 上最后一条 role==user 的 MESSAGE entry id——compaction 的 kept-tail 起点
        （docs/14 §4.4 / bug#1：firstKeptEntryId 必须是被保留 tail 的起点，而非压缩时的 live leaf；
        否则 fold 的两区折叠会丢掉本应保留的 last-user 之后的近期上下文）。无 user 消息 → None。"""
        last = None
        for e in self.get_branch(leaf_id):
            if e.type == tree.MESSAGE and (e.data.get("message") or {}).get("role") == "user":
                last = e.id
        return last

    def build_context(self, leaf_id: str | None = None) -> BuiltContext:
        branch = self.get_branch(leaf_id)
        rich, scalar = context.fold(branch)
        return BuiltContext(messages=context.convert_to_llm(rich), scalar=scalar)

    def labels(self) -> dict[str, str]:
        return tree.labels_by_id(self._entries)

    def name(self) -> str | None:
        return tree.session_name(self._entries)

    def parent_session(self) -> dict | None:
        """本 session 的 parentSession 回指（child 侧权威，docs/13 §7.2）。"""
        for e in self._entries:
            if e.type == tree.SESSION_START:
                ps = e.data.get("parentSession")
                return ps if isinstance(ps, dict) else None
        return None

    # ── fork / clone（docs/13 §7：in-file vs 跨文件） ───────────────────────────
    def clone(self, from_entry_id: str | None = None, *, new_session_id: str | None = None) -> "SessionManager":
        """跨文件 clone：新 session 复制本 session 从 from_entry 到 root 的 path-to-root
        （Pi repo.fork **会**复制史，评审 M6），header 记 parentSession 血缘。

        鲁棒处理 pre-3a 与 post-3a 两种盘上树：剥掉 branch 里的 session_start（header，child 自有），
        并把任何指向被剥 session_start 的 parentId 重置为 None——使首条消息成为 child 干净的 branch
        root，避免 child 文件出现两条 session_start（docs/14 P3 review）。"""
        leaf = from_entry_id if from_entry_id is not None else self.get_leaf()
        branch = self.get_branch(leaf)
        start_ids = {e.id for e in branch if e.type == tree.SESSION_START}
        msgs = [e for e in branch if e.type != tree.SESSION_START]
        if not msgs:
            raise tree.SessionTreeError("nothing to clone")
        child = SessionManager.create(
            new_session_id,
            cwd=self._cwd(),
            parent_session={"sessionId": self.session_id, "entryId": leaf},
            lock=False,                 # bootstrap-then-handoff：child 由 runtime lease 重新加锁打开
        )
        for e in msgs:
            parent = None if e.parentId in start_ids else e.parentId
            child._persist_new(Entry(type=e.type, id=e.id, parentId=parent,
                                     sessionId=child.session_id, timestamp=e.timestamp, data=e.data))
        return child

    def _cwd(self) -> str:
        for e in self._entries:
            if e.type == tree.SESSION_START:
                return e.data.get("cwd", os.getcwd())
        return os.getcwd()

    def rewrite_file(self) -> None:
        """整文件原子重写（GC/紧凑；Pi `_rewriteFile`）。当前写出全部 in-memory entries。"""
        self._require_writer()
        path = session_file(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in self._entries:
                f.write(json.dumps(e.to_dict(), ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, path)


# ─── 跨 session 导航（child 侧 session_start.parentSession 回指为权威，docs/13 §7.2/§11） ──
def _scan_headers() -> list[tuple[str, dict | None]]:
    """枚举所有 session 的 (session_id, parentSession)，只读 session.jsonl 首行（cheap listing）。"""
    out: list[tuple[str, dict | None]] = []
    root = sessions_dir()
    if not root.exists():
        return out
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        path = entry / "session.jsonl"
        if not path.exists():
            continue
        try:
            first = path.open(encoding="utf-8").readline().strip()
            d = json.loads(first) if first else {}
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("type") == tree.SESSION_START:
            ps = d.get("data", {}).get("parentSession")
            out.append((d.get("sessionId", entry.name), ps if isinstance(ps, dict) else None))
    return out


def children(parent_session_id: str) -> list[str]:
    """parent 的所有 child session id（权威 = child header 回指扫描，branch-independent，
    survives 父 abort/rewind/fork——评审命中）。"""
    return sorted(sid for sid, ps in _scan_headers()
                  if ps and ps.get("sessionId") == parent_session_id)


def parent_of(child_session_id: str) -> str | None:
    for sid, ps in _scan_headers():
        if sid == child_session_id:
            return ps.get("sessionId") if ps else None
    return None


def siblings(child_session_id: str) -> list[str]:
    p = parent_of(child_session_id)
    if p is None:
        return []
    return [sid for sid in children(p) if sid != child_session_id]
