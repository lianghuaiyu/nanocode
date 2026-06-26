"""tools/types.py — 工具系统的能力 / 信任 / 来源枚举(docs/24 §4.1)。

Phase 1 仅声明、不强制:`Tool.needs`/`trust`/`source` 是惰性元数据,dispatch 尚未消费。
Phase 2 起 dispatch 按「声明 ∩ 信任档策略」铸造能力把手进 ToolContext。
"""

from __future__ import annotations

from enum import Enum


class Capability(Enum):
    """工具可声明的能力(最小授权核心)。emit/ask/abort 属 per-call 核心,人人都有,不在声明里。"""

    EXEC = "exec"
    FS_READ = "fs:read"
    FS_WRITE = "fs:write"
    SPAWN = "spawn"
    MEMORY = "memory"
    TASKS = "tasks"
    SESSION_READ = "session:read"
    MODELS = "models"
    SET_MODE = "set_mode"


class Trust(Enum):
    """工具信任分档(docs/24 §1)。默认不可信、可信 opt-in。"""

    BUILTIN = "builtin"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class ToolSource(Enum):
    """工具来源(provenance;docs/24 §4.5 四条注册路径)。"""

    BUILTIN = "builtin"
    MCP = "mcp"
    EXT = "ext"
    EMBEDDER = "embedder"


# ─── 信任档能力策略表(docs/24 §1 / §4.3，纯表、import-light)──────────────────────
# 铸造规则(engine._granted_capabilities)：granted = tool.needs ∩ policy_for_trust(tool.trust)。
# BUILTIN「声明什么给什么」(把手仍沙箱中介)；TRUSTED 仅读类;UNTRUSTED 零宿主能力
# (外部工具默认只剩 per-call 核心 emit/ask/abort)。

_TRUST_POLICY: dict[Trust, frozenset[Capability]] = {
    Trust.BUILTIN: frozenset(Capability),                       # 全集
    Trust.TRUSTED: frozenset({                                  # 读类
        Capability.FS_READ, Capability.TASKS, Capability.SESSION_READ,
    }),
    Trust.UNTRUSTED: frozenset(),                               # 空——零宿主能力
}


def policy_for_trust(trust: Trust) -> frozenset[Capability]:
    """信任档允许铸造的能力上限(docs/24 §4.3)。

    BUILTIN → 全集;TRUSTED → {FS_READ, TASKS, SESSION_READ};UNTRUSTED → ∅。
    与 tool.needs 取交得最终授予集。
    """
    return _TRUST_POLICY[trust]
