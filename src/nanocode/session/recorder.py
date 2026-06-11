"""session/recorder.py — P2 双写脚手架：主 agent 每个 turn 的 live provider 列表 → canonical 树。

迁移期与既有 flat/v2/wire 持久化**并存、非权威**。全程 guarded：任何异常都不得破坏 live turn。
已知脚手架限制（docs/13 §9-P2，由 P4 compaction-as-entry 接管）：
  - 捕获 post-compression 列表（snip 后内容）——故闸门只对无压缩 turn 成立；
  - live list 变短（compaction 整列替换）→ 跳过该轮 tail-append，不破坏既有树。
"""

from __future__ import annotations

from . import capture, tree
from .manager import SessionManager


class TurnRecorder:
    """每个主 session 一个：把 turn-end 的 provider 列表 tail-append 进 session.jsonl 树。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._mgr: SessionManager | None = None
        self._recorded = 0

    def _manager(self) -> SessionManager:
        if self._mgr is None:
            if SessionManager.exists(self.session_id):
                self._mgr = SessionManager.open(self.session_id)
                # resume 安全：按已有 message entry 数对齐 recorded，避免重复 append。
                self._recorded = sum(1 for e in self._mgr.entries() if e.type == tree.MESSAGE)
            else:
                self._mgr = SessionManager.create(self.session_id)
        return self._mgr

    def record_turn(self, provider: str, provider_messages: list, *, model: str = "") -> None:
        neutral = capture.capture_provider_messages(provider_messages, provider, model=model)
        if len(neutral) < self._recorded:
            return  # 列表缩短（compaction 替换）→ 脚手架跳过；P4 由 compaction-as-entry 接管
        mgr = self._manager()
        for msg in neutral[self._recorded:]:
            mgr.append_message(msg)
        self._recorded = len(neutral)
        leaf = mgr.get_leaf()
        if leaf is not None:
            mgr.set_leaf(leaf)  # turn-end 显式落一条 leaf 标边界（docs/13 §9-P2）
