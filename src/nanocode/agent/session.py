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

    def resume(self, *, agent_id: str = "main") -> list:
        """经 SessionContextBuilder 取 resume 上下文并装入 agent 的 MessageStore（不覆盖空）。

        等价于旧 restore 的「有数据才装」语义；P5 换成事件树重建时此调用点不变。
        """
        messages = self.context_builder.resume_messages(agent_id=agent_id)
        if messages:
            self.agent._load_messages(messages)
        return messages
