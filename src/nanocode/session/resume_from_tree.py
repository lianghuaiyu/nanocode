"""session/resume_from_tree.py — P3：从 canonical `session.jsonl` 树重建 resume 上下文。

open SessionManager → build_context（fold+convert_to_llm）→ render 成当前 provider 列表。
有树且非空才返回 provider 列表；无树/空 → None（调用方回退既有 wire-rebuild/snapshot 路径）。
纯读、无副作用；调用方负责 guard。docs/13 §9-P3（评审 M3：树优先但快照降级兜底、不删）。
"""

from __future__ import annotations

from .manager import SessionManager
from .render import ModelCtx, render

# provider → api（与 capture.py 默认一致，使 render 的 isSameModel 判定稳定）
_API = {"anthropic": "anthropic", "openai": "openai-completions"}


def resume_from_tree(
    session_id: str,
    *,
    provider: str,
    model: str = "",
    system_prompt: str | None = None,
    supports_images: bool = True,
) -> list[dict] | None:
    """从树重建 → 当前 provider 的消息列表；无树/空上下文 → None。"""
    if not SessionManager.exists(session_id):
        return None
    mgr = SessionManager.open(session_id)
    built = mgr.build_context()
    if not built.messages:
        return None
    ctx = ModelCtx(provider=provider, api=_API.get(provider, provider),
                   model_id=model or "", supports_images=supports_images)
    return render(built.messages, ctx, system_prompt=system_prompt)["messages"]
