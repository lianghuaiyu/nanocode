"""AgentSession：Pi 化的内部会话对象——拥有「一次用户输入 = 一个 turn」的生命周期。

P3 seam：AgentSession 包住 AgentCore（现 Agent），run_turn() 负责一个 turn 的编排外壳
（append user event → 构造上下文 → 跑模型循环 → append result events → 持久化）。本步**不改
行为**：turn 边界语义（begin_turn / user_message / 模型循环 / turn_end / _auto_save /
CancelledError→_aborted）今天就在 Agent.chat 内，run_turn 委托之；模型循环本身留在 AgentCore。

它不负责 JSON-RPC，也不负责 CLI 渲染（那是 P4 的 RuntimeThread / 表现层 sink）。上下文来源
经 SessionContextBuilder（P3 快照、P5 事件树重建），故 resume/fork 走同一入口。
"""

from __future__ import annotations

from .context_builder import SessionContextBuilder


class AgentSession:
    """会话层：拥有 agent 的 turn 生命周期与 resume 上下文入口。

    刻意是薄包装——把「会话编排」从「模型循环」(AgentCore=Agent) 名义上分离，为 P4
    RuntimeThread / P5 事件重建提供稳定 seam，且当前零行为变更（run_turn 委托 agent.chat）。
    """

    def __init__(self, agent, *, context_builder: "SessionContextBuilder | None" = None) -> None:
        self.agent = agent
        self.context_builder = context_builder or SessionContextBuilder(agent.session_id)

    @property
    def session_id(self) -> str:
        return self.agent.session_id

    @property
    def aborted(self) -> bool:
        return self.agent._aborted

    async def run_turn(self, prompt: str) -> None:
        """跑一个 turn：当前委托 AgentCore.chat（其已含 begin_turn/user_message/模型循环/
        turn_end/_auto_save/取消语义）。返回 None——最终文本经 sink 呈现（主 agent）或经
        run_once 的 BufferSink 捕获（子 agent）；结构化结果由 P4 的 TurnResult 承载。"""
        await self.agent.chat(prompt)

    def resume(self, *, agent_id: str = "main", prefer_events: bool = True) -> list:
        """经 SessionContextBuilder 取 resume 上下文并装入 agent 的 MessageStore（不覆盖空）。

        P5：默认 prefer_events=True——events 成为 resume 权威（从 wire leaf→root 重建，用
        llm_request 快照作 byte-exact oracle），snapshot 降为兜底 cache（重建为空时回退，
        不丢数据）。等价于旧 restore 的「有数据才装」语义；调用方可传 prefer_events=False 强制快照。
        """
        messages = self.context_builder.resume_messages(agent_id=agent_id, prefer_events=prefer_events)
        if messages:
            self.agent._load_messages(messages)
        return messages

    def fork_to(self, from_event_id: str, branch_id: str, *, agent_id: str = "main") -> list:
        """把本 session 切到一个新分支（fork）：从 from_event_id 重建上下文装入，并让 tracer
        在新 branch_id 下继续（首事件带 parent_event_id=from_event_id）。返回重建的上下文。

        后续 run_turn 追加到新分支，不覆盖原分支（append-only + branch_id 隔离）。
        """
        messages = self.context_builder.rebuild_messages(agent_id=agent_id, leaf_id=from_event_id)
        self.agent.tracer.begin_branch(branch_id, from_event_id=from_event_id)
        self.agent._load_messages(messages)
        return messages
