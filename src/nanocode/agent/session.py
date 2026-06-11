"""AgentSession：Pi 化的内部会话对象——拥有「一次用户输入 = 一个 turn」的生命周期。

run_turn() 负责一个 turn 的编排外壳（委托 AgentCore=Agent.chat：begin_turn / user_message /
模型循环 / turn_end / _auto_save / 取消语义）；move_to() 做 in-file 导航（canonical 树 leaf）。

docs/14 SessionLease：原 SessionContextBuilder（wire/snapshot resume + 事件树 fork）与
`Agent.restore_session` 均已退役——resume 由 runtime 激活会话写者租约（SessionLease.open + rebind /
cli._load_from_manager）从 canonical session.jsonl 重建；in-file /fork、/clear、/tree 经本对象的
move_to（移 leaf），跨文件 /clone 经 runtime.thread_clone；二者都不再读 legacy flat 快照。
"""

from __future__ import annotations


class AgentSession:
    """会话层：拥有 agent 的 turn 生命周期与 in-file 导航。薄包装——把「会话编排」从「模型循环」
    (AgentCore=Agent) 名义上分离，为 RuntimeThread / 表现层 sink 提供稳定 seam。"""

    def __init__(self, agent) -> None:
        self.agent = agent

    @property
    def session_id(self) -> str:
        return self.agent.session_id

    @property
    def aborted(self) -> bool:
        return self.agent._aborted

    async def run_turn(self, prompt: str) -> None:
        """跑一个 turn：委托 AgentCore.chat（其已含 begin_turn/user_message/模型循环/turn_end/
        _auto_save/取消语义）。返回 None——最终文本经 sink 呈现或经 run_once 的 BufferSink 捕获；
        结构化结果由 RuntimeThread 的 TurnResult 承载。"""
        await self.agent.chat(prompt)

    def move_to(self, entry_id: "str | None", *, agent_id: str = "main") -> list:
        """docs/14 P6：把 active leaf 移到 canonical 树的 `entry_id`（in-file 导航 / checkout / fork-before）。
        `entry_id=None` → 复位到 root（空上下文；/fork 选中第一条消息、/clear 等用）。从该 leaf 重建上下文
        并装入 live agent。导航即日志（写一条 leaf entry），后续 turn 在此 leaf 下追加（in-file 分支，不覆盖
        原分支）。fail-closed：无 active 写者租约 / entry 不存在 → ValueError，不动会话。返回重建的消息列表。"""
        from ..session.render import ModelCtx, render
        a = self.agent
        mgr = a._session_mgr
        if mgr is None:
            # docs/14 SessionLease：in-file 导航是写操作（set_leaf），必须经 active 写者租约——
            # 缺租约 fail-closed，不再 lazy 打开未加锁 mgr。
            raise ValueError("no active session writer lease; cannot navigate the tree")
        if entry_id is not None and entry_id not in {e.id for e in mgr.entries()}:
            raise ValueError(f"entry '{entry_id}' not found in session tree; session left unchanged")
        mgr.set_leaf(entry_id)                  # None → 复位到 root
        built = mgr.build_context()
        provider = "openai" if a.use_openai else "anthropic"
        api = "openai-completions" if a.use_openai else "anthropic"
        sysp = a._system_prompt if a.use_openai else None
        msgs = render(built.messages, ModelCtx(provider=provider, api=api, model_id=a.model),
                      system_prompt=sysp)["messages"]
        a._load_messages(msgs)
        return msgs
