"""SessionContextBuilder：构造一个 agent 的会话上下文（resume / fork 起点的 provider 消息）。

P3（本步）：source = snapshot（v2 messages.json / flat JSON）——与现有 restore 行为等价，
仅把「上下文从哪来」抽到稳定入口。P5 将把 source 换成 event tree（wire）leaf→root 重建
（含 compaction supersession + tool_use/tool_result 配对），入口签名不变。

这样 AgentSession / 子 agent resume / 未来 fork 都经同一入口取上下文，P5 只换实现不动调用方。
"""

from __future__ import annotations

import copy

from ..session import v2 as _session_v2
from ..events import reader as _event_reader


class SessionContextBuilder:
    """按 session 构造 agent 的 resume / fork 上下文。

    两种 source：
    - snapshot（P3 起点）：v2 目录 messages.json，与旧 restore 等价。
    - event tree（P5）：per-agent wire leaf→root，用 llm_request 快照作 byte-exact oracle
      （它记录每轮**模型实际看到**的消息数组，post-compression，故无需重放 tier 标记/compaction）
      + 其后的 assistant_message 重建尾条助手消息。

    `resume_messages` 默认优先事件树重建、快照兜底（defensive：重建为空则不丢数据）。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    # ─── snapshot source（P3）────────────────────────────────

    def snapshot_messages(self, *, agent_id: str = "main") -> list:
        if agent_id == "main":
            return _session_v2.read_main_messages(self.session_id)
        return _session_v2.read_agent_messages(self.session_id, agent_id)

    # ─── event-tree source（P5）──────────────────────────────

    def rebuild_messages(self, *, agent_id: str = "main", leaf_id: "str | None" = None) -> list:
        """从该 agent 的 wire 事件树重建 provider 消息数组。

        leaf_id=None → 用该 agent wire 的最后一条事件作 leaf（resume 当前分支）；
        否则从指定 event id 沿 parent_id 走到 root（fork 起点）。
        重建 = 选定分支上最后一个 llm_request 的 messages（byte-exact 模型所见）
              + 其后追加的 assistant 文本消息（按 provider 形态重建）。
        无 llm_request → 返回 []（调用方兜底快照）。
        """
        messages, _faithful = self._rebuild(agent_id=agent_id, leaf_id=leaf_id)
        return messages

    def _rebuild(self, *, agent_id: str = "main", leaf_id: "str | None" = None):
        """重建 + 忠实度判定，返回 (messages, faithful)。

        faithful=False 表示：最后一个 llm_request 之后存在**未闭合的 tool 轮**
        （assistant 带 tool_uses，或其后跟 tool_result，却没有下一个 llm_request 把它纳入快照）
        ——这类 turn（abort / budget / turn-limit / context-break 打断）下，重建会丢掉那条
        assistant tool_use 及其 tool_result（Codex/workflow 标的 blocking 数据丢失）。调用方据此
        回退到 snapshot（_auto_save 始终写了完整列表），保证 resume 翻转**不丢数据**。
        """
        wire = _session_v2.agent_wire_path(self.session_id, agent_id)
        events = _event_reader.read_agent_wire(wire, agent_id)
        if not events:
            return [], False
        branch = _branch_chain(events, leaf_id)
        last_req_idx = _last_index(branch, lambda e: e.type == "llm_request" and isinstance(e.data.get("messages"), list))
        if last_req_idx is None:
            return [], False
        tail = branch[last_req_idx + 1:]
        messages = copy.deepcopy(branch[last_req_idx].data["messages"])
        trailing = _trailing_assistant(tail)
        if trailing is not None:
            messages.append(trailing)
        # 忠实度：尾部出现「带 tool_uses 的 assistant」或「tool_result」即为未闭合 tool 轮，
        # 这部分未进任何 llm_request 快照、也不会被 _trailing_assistant 重建 → 不忠实。
        faithful = not any(
            (e.type == "assistant_message" and e.data.get("tool_uses")) or e.type == "tool_result"
            for e in tail
        )
        return messages, faithful

    def resume_messages(self, *, agent_id: str = "main", prefer_events: bool = False) -> list:
        """resume 上下文。默认 snapshot（保持 P3 行为，resume 权威仍是快照）。

        `prefer_events=True`：事件树重建优先，但**仅当重建忠实**（无未闭合 tool 轮）才采用；
        否则回退 snapshot（_auto_save 始终写了完整列表）——保证 resume 翻转无数据丢失。
        """
        if prefer_events:
            rebuilt, faithful = self._rebuild(agent_id=agent_id)
            if rebuilt and faithful:
                return rebuilt
        return self.snapshot_messages(agent_id=agent_id)

    def current_branch(self, *, agent_id: str = "main", leaf_id: "str | None" = None) -> "str | None":
        """返回 resume leaf 所在的 branch_id（供 restore 时让 tracer 续在正确分支上）。

        leaf_id 指定时返回该 event 的 branch；否则返回最后一条**会话**事件的 branch——
        跳过尾部的 session_start（resume 时新构造的 agent 会在 __init__ 往同一 wire 追加一条
        session_start，它恒在默认 main 分支，不能用它判分支，否则 fork 续写被误记为 main）。
        无事件→None。修复 Codex review P2。
        """
        wire = _session_v2.agent_wire_path(self.session_id, agent_id)
        events = _event_reader.read_agent_wire(wire, agent_id)
        if not events:
            return None
        if leaf_id is not None:
            for e in events:
                if e.id == leaf_id:
                    return e.branch_id or "main"
            return None
        for e in reversed(events):
            if e.type != "session_start":
                return e.branch_id or "main"
        return events[-1].branch_id or "main"


def _branch_chain(events: list, leaf_id: "str | None") -> list:
    """返回从 leaf 沿 parent_id 到 root 的事件链（按 seq 升序）。

    leaf_id=None → 全部事件（单分支，按读入顺序=seq 序）。否则从 leaf_id 回溯 parent_id，
    收集祖先集合，过滤出链上事件（保 seq 序）——支持 fork：只取目标分支可达的事件。
    """
    if leaf_id is None:
        return list(events)
    by_id = {e.id: e for e in events}
    chain_ids: set = set()
    cur = leaf_id
    seen: set = set()
    while cur and cur in by_id and cur not in seen:
        seen.add(cur)
        chain_ids.add(cur)
        cur = by_id[cur].parent_id
    return [e for e in events if e.id in chain_ids]


def _last_index(events: list, pred) -> "int | None":
    for i in range(len(events) - 1, -1, -1):
        if pred(events[i]):
            return i
    return None


def _trailing_assistant(events: list) -> "dict | None":
    """从最后一个 llm_request 之后的事件里取尾条 assistant 的**文本响应**，重建为文本消息。

    仅处理 turn 收尾的纯文本助手响应（带 tool_uses 的不在此——那种「未闭合 tool 轮」由
    _rebuild 的 faithful 判定拦下、回退 snapshot）。**刻意规范化为 text-only**
    `{role:assistant, content:<str>}`（两 provider 同形）：与 API 等价（Anthropic 接受 str
    content，compaction ack 已如此用），且 thinking/签名等已在落盘时剥离，故不丢模型行为。
    无文本则 None（不追加）。
    """
    idx = _last_index(events, lambda e: e.type == "assistant_message")
    if idx is None:
        return None
    ev = events[idx]
    if ev.data.get("tool_uses"):
        return None  # 有工具的 assistant 不是 turn 收尾条；其未闭合性由 faithful 判定处理
    text = ev.data.get("text") or ""
    if not text:
        return None
    # text-only 规范化：str content 对 Anthropic / OpenAI 均合法（见 docstring）。
    return {"role": "assistant", "content": text}

