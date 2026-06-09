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
        wire = _session_v2.agent_wire_path(self.session_id, agent_id)
        events = _event_reader.read_agent_wire(wire, agent_id)
        if not events:
            return []
        branch = _branch_chain(events, leaf_id)
        # 最后一个 llm_request 的 messages = 该点模型所见的完整数组（post-compression）。
        last_req_idx = _last_index(branch, lambda e: e.type == "llm_request" and isinstance(e.data.get("messages"), list))
        if last_req_idx is None:
            return []
        messages = copy.deepcopy(branch[last_req_idx].data["messages"])
        # 追加最后一个 llm_request 之后的尾条 assistant 文本（turn 收尾的响应，未进任何 llm_request 快照）。
        trailing = _trailing_assistant(branch[last_req_idx + 1:])
        if trailing is not None:
            messages.append(trailing)
        return messages

    def resume_messages(self, *, agent_id: str = "main", prefer_events: bool = False) -> list:
        """resume 上下文。默认 snapshot（保持 P3 行为，resume 权威仍是快照）。

        `prefer_events=True` 改为事件树重建优先、快照兜底——resume 权威的「翻转」是 task 21
        的刻意一步（带 byte-equal 校验 + 兜底），不在此默认开启，避免静默改变数据敏感的 resume。
        """
        if prefer_events:
            rebuilt = self.rebuild_messages(agent_id=agent_id)
            if rebuilt:
                return rebuilt
        return self.snapshot_messages(agent_id=agent_id)


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
    """从最后一个 llm_request 之后的事件里取尾条 assistant 文本消息，按 provider 形态重建。

    仅处理 turn 收尾的纯文本助手响应（无 tool_uses——有 tool 的轮次其后必有新 llm_request，
    其快照已含该 assistant，不会落到尾部）。OpenAI: {role,content:str}；Anthropic: content 文本块。
    无文本则 None（不追加）。
    """
    idx = _last_index(events, lambda e: e.type == "assistant_message")
    if idx is None:
        return None
    ev = events[idx]
    if ev.data.get("tool_uses"):
        return None  # 有工具的 assistant 不是 turn 收尾条，其后有 llm_request 兜住
    text = ev.data.get("text") or ""
    if not text:
        return None
    # provider 形态：anthropic 助手 content 为块列表，openai 为字符串。
    # 经 branch 上既有消息形态推断：若 llm_request.messages 的助手项 content 是 list → anthropic。
    return {"role": "assistant", "content": text}

