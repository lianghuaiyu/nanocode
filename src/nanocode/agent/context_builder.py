"""SessionContextBuilder：构造一个 agent 的会话上下文（resume / fork 起点的 provider 消息）。

P3（本步）：source = snapshot（v2 messages.json / flat JSON）——与现有 restore 行为等价，
仅把「上下文从哪来」抽到稳定入口。P5 将把 source 换成 event tree（wire）leaf→root 重建
（含 compaction supersession + tool_use/tool_result 配对），入口签名不变。

这样 AgentSession / 子 agent resume / 未来 fork 都经同一入口取上下文，P5 只换实现不动调用方。
"""

from __future__ import annotations

from ..session import v2 as _session_v2


class SessionContextBuilder:
    """按 session 构造 agent 的 resume 上下文。P3：快照读取；P5：事件树重建。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def resume_messages(self, *, agent_id: str = "main") -> list:
        """返回某 agent 的 provider 消息列表（resume / fork 起点）。

        P3 实现：从 v2 目录快照读（main/messages.json 或 agents/<id>/messages.json）。
        返回空列表表示无快照——调用方据此决定不覆盖（保持旧 restore 的 if-data 语义）。
        """
        if agent_id == "main":
            return _session_v2.read_main_messages(self.session_id)
        return _session_v2.read_agent_messages(self.session_id, agent_id)
