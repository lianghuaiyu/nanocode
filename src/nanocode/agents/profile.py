"""agents/profile.py — AgentProfile + 子策略（docs/15 §10）。

正式取代 subagents/config.py 的 dict。一个 profile 完整描述一个 agent：mode / model / 工具 /
权限 / context 行为 / skills / MCP / memory / hooks / isolation / hidden。built-in 与自定义
agent 都解析成 AgentProfile（Phase 5 registry 负责发现 + extends 收窄）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# profile mode（§10）：primary 可作主 agent；subagent 仅被 spawn；system 宿主内部（curator/title/
# summary/compaction）不向模型暴露；all = 既可主又可子。
AgentMode = Literal["primary", "subagent", "system", "all"]
AGENT_MODES: tuple[str, ...] = ("primary", "subagent", "system", "all")


@dataclass
class PermissionProfile:
    """profile 的权限派生输入（§11.3：child≤parent，deny 并集，allow 交集）。

    - mode：default / plan / acceptEdits / bypassPermissions（与 PermissionEngine 的 PermissionMode 对齐）。
    - tools_allow=None 表示无 allow-list（除显式 deny 外全部）；非 None 则收窄到该集合。
    - tools_deny：始终剔除（deny 胜出）。
    - auto_deny_confirms：后台 agent 无 TTY,需确认的危险调用一律 auto-deny。
    """

    mode: str = "default"
    tools_allow: set[str] | None = None
    tools_deny: set[str] = field(default_factory=set)
    auto_deny_confirms: bool = False


@dataclass
class ContextProfile:
    """profile 的 context 行为（§8 / §9.2）：codeintel 开关、repo-map 预算、是否注入 memory/skills。"""

    codeintel: bool = True
    repo_map: bool = True
    repo_map_budget_tokens: int = 1024
    inject_memory: bool = True
    inject_skills: bool = True
    inject_project_instructions: bool = True


@dataclass
class MemoryPolicy:
    """memory scope：是否可读/写宿主 memory、是否参与 recall。"""

    recall: bool = True
    write: bool = False


@dataclass
class HookPolicy:
    """hook scope：是否允许 skill hook 在本 agent 内注册/执行。"""

    allow_skill_hooks: bool = True


@dataclass
class IsolationPolicy:
    """isolation：child session 隔离 + 是否可 spawn 后代 + 全局深度预算。"""

    own_session: bool = True
    can_spawn: bool = False          # 子 agent 默认不可 spawn 孙（§11.3）
    max_depth: int | None = None


@dataclass
class McpServerRef:
    """profile 引用的 MCP server（按名;具体连接由 capabilities/mcp 解析）。"""

    name: str


@dataclass
class AgentProfile:
    """一个 agent 的完整 profile（§10）。built-in + 自定义 .md 都解析成它。"""

    name: str
    description: str = ""
    mode: AgentMode = "subagent"
    prompt: str = ""
    model: str | None = None
    thinking: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_turns: int | None = None
    timeout_ms: int | None = None
    tools_allow: set[str] | None = None
    tools_deny: set[str] = field(default_factory=set)
    spawn_allow: set[str] | None = None
    permission: PermissionProfile = field(default_factory=PermissionProfile)
    context: ContextProfile = field(default_factory=ContextProfile)
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[McpServerRef] = field(default_factory=list)
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    hooks: HookPolicy = field(default_factory=HookPolicy)
    isolation: IsolationPolicy = field(default_factory=IsolationPolicy)
    source: str | None = None        # 自定义 agent 的 .md 路径（审计 + 同名碰撞排查）
    hidden: bool = False             # system agent 不向模型暴露为可 spawn 类型

    def effective_tool_names(self, universe: set[str]) -> set[str]:
        """在给定全量工具名 universe 上算有效集：allow 过滤 → deny 剔除 → 永远剔除 'agent'（除非
        isolation.can_spawn）。与 subagents.config._filter_tools 同语义（§10/§11.3）。"""
        names = set(universe) if self.tools_allow is None else (set(self.tools_allow) & universe)
        names -= set(self.tools_deny)
        if not self.isolation.can_spawn:
            names.discard("agent")
        return names

    def is_spawnable(self) -> bool:
        """是否可经 agent 工具被 spawn（subagent/all,且非 hidden/system）。"""
        return self.mode in ("subagent", "all") and not self.hidden
