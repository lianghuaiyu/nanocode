"""context/cache_policy.py — prompt cache 稳定性策略（docs/15 §8.3 / §8.4）。

借 Claude Code 思路：稳定全局 system 文本在前、工具 schema 确定性排序、动态 system 文本最小化、
项目/用户上下文作 messages/custom_message 而非可变 system、model/thinking/tool-set 变更是**显式
cache-breaking 事件**。本模块只做 provider-中立的分类与判定；provider-specific 的 Anthropic cache
control 留在 provider adapter，不在这里。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .packs import ContextPack

CACHE_POLICIES = ("stable_prefix", "append_only", "volatile_tail")
PERSIST_POLICIES = ("none", "custom_message", "message", "derived_only")
LIFECYCLES = ("session", "turn", "until_compact", "path_triggered", "one_shot", "manual")


def breaks_stable_prefix(packs: "list[ContextPack]") -> bool:
    """这批 packs 里是否有任何一个会破坏稳定前缀缓存。

    只有 cache_policy=="stable_prefix" 的 pack 进稳定前缀；append_only 追加在前缀**之后**（不破）、
    volatile_tail 在尾部（不破）。判定 True 当且仅当存在一个 pack 自称要进 stable_prefix 但其
    lifecycle 不是 session（即它其实会变）——这种错配会让"稳定前缀"每轮变化、击穿缓存。
    """
    for p in packs:
        if p.cache_policy == "stable_prefix" and p.lifecycle != "session":
            return True
    return False


def survives_compaction(pack: "ContextPack") -> bool:
    """compaction 后该 pack 是否存活（docs/15 §8.4 survival matrix）。

    规则：
    - session 生命周期 → 存活（整会话不变，如稳定 system / root 项目指令重载）。
    - persist_policy 是 message / custom_message 且 lifecycle 是 until_compact → 不存活（定义上到压缩为止）。
    - turn / one_shot / manual → 不存活（单轮或一次性）。
    - path_triggered → 不存活（需路径再次触发重新注入）。
    其余按 lifecycle 保守判定。
    """
    if pack.lifecycle == "session":
        return True
    if pack.lifecycle in ("turn", "until_compact", "path_triggered", "one_shot", "manual"):
        return False
    return False
