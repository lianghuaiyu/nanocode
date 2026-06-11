"""context/packs.py — ContextPack（docs/15 §8.1）。

每个被注入的上下文来源（项目指令、git 快照、当前日期、memory recall、skill listing/body、
MCP/deferred 工具公告、repo map、后台任务完成、文件变更提醒、compaction summary、agent profile
指令…）都封装成一个结构化 ContextPack，而非裸字符串。这使 ContextLedger 能记账、预算能决策、
`/context` 能审计、survival matrix 能定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Lifecycle = Literal["session", "turn", "until_compact", "path_triggered", "manual"]
CachePolicy = Literal["stable_prefix", "append_only", "volatile_tail"]
PersistPolicy = Literal["none", "custom_message", "message", "derived_only"]


def estimate_tokens(content: str | list[dict]) -> int:
    """粗略 token 估计（~4 chars/token）。validate against LLM_REQUEST 的 messagesChars（B 遥测）。

    list[dict]（block 形态）只计 text 字段字符——image/二进制 block 不按字符计。
    """
    if isinstance(content, str):
        return max(1, len(content) // 4)
    if isinstance(content, list):
        chars = 0
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                chars += len(b.get("text", ""))
            elif isinstance(b, str):
                chars += len(b)
        return max(1, chars // 4)
    return 0


@dataclass
class ContextPack:
    """一个结构化的上下文注入单元（docs/15 §8.1）。

    字段语义：
    - lifecycle：何时失效（session 整会话 / turn 单轮 / until_compact 到下次压缩 /
      path_triggered 触碰路径后 / manual 手动）。
    - provenance：来源（provider id、文件路径、触发原因等），供 /context 审计。
    - cache_policy：对 prompt cache 的影响（stable_prefix 进稳定前缀、append_only 追加不破前缀、
      volatile_tail 每轮可变尾部）。
    - persist_policy：是否/如何落进 canonical 树（none 纯 ephemeral / custom_message 作 custom_message
      entry / message 作普通 message / derived_only 仅派生遥测不入 fold）。
    """

    id: str
    kind: str
    content: str | list[dict]
    lifecycle: Lifecycle = "turn"
    provenance: dict = field(default_factory=dict)
    token_estimate: int = 0
    priority: int = 0
    cache_policy: CachePolicy = "volatile_tail"
    persist_policy: PersistPolicy = "custom_message"

    def __post_init__(self) -> None:
        if not self.token_estimate:
            self.token_estimate = estimate_tokens(self.content)

    def as_text(self) -> str:
        """content 归一为字符串（list[dict] block → 拼接 text）。"""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            return "".join(
                b.get("text", "") for b in self.content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""

    def to_custom_message(self, *, display: bool = False) -> dict:
        """渲染成 canonical 树 custom_message entry 的 data（AgentSession.record_event 用）。

        custom_message 在 context.convert_to_llm 里**原样无 PREFIX** 折成 user 消息——绝不改写
        last user 消息（docs/15 §8.5）。display=False = 对 LLM 可见但 UI 不重复渲染。
        """
        return {"customType": self.kind, "content": self.content, "display": display}
