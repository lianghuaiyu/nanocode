"""agents/registry.py — AgentProfile 发现 / 解析（docs/15 §10 / docs/16 #7）。

subagents/config.py 已退役：发现（用户/项目级 `.nanocode/agents` + vendor-neutral `.agents/agents`
+ P4 trust gate + (cwd, trusted) 键控缓存）、extends 收窄代数（`_resolve_effective`，fail-closed）、
工具过滤语义（allow ∩ → deny − → 永剔 'agent'）**整体 port 进本模块**，dict API 删除——
spawn / 显示面 / 测试一律走 typed `AgentProfile`（build_profile / effective_tools）。

allow-list 交集语义与 child 派生共用 agents.permissions.intersect_allow（docs/16 §2#4：
两份重复实现合一）。

built-in profiles（§10）：
- build：主 write-capable primary；plan：write/shell 受限 primary；
- explore：只读 subagent；general/coder：write-capable subagent；
- system（memory curators）：hidden,不向模型暴露为可 spawn。
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..frontmatter import parse_frontmatter, as_list
from ..paths import data_dir, project_config_dir
from .permissions import intersect_allow
from .profile import (
    AgentProfile, ContextProfile, IsolationPolicy, PermissionProfile,
)

# ─── Read-only tools (for explore and plan agents) ──────────

READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search"}

# extends 解析的最大深度（防御环/恶意深链）。
_MAX_EXTENDS_DEPTH = 5

# 内置基类型（extends 可指向它们）。
_BUILTIN_BASE_TYPES = {"explore", "plan", "general", "coder"}

# 保留类型由宿主特殊调度（记忆巩固 curator），不能被项目/用户级 .md 覆盖，
# 也不向模型暴露为可 spawn 的 agent type。
from ..subagents.prompts import (  # noqa: E402
    EXPLORE_PROMPT, PLAN_PROMPT, GENERAL_PROMPT,
    MEMORY_CURATOR_TYPE, MEMORY_EVAL_CURATOR_TYPE, CURATOR_EVAL_PROMPT,
)

RESERVED_AGENT_TYPES = frozenset({MEMORY_CURATOR_TYPE, MEMORY_EVAL_CURATOR_TYPE})

# ─── Custom agent discovery（自 subagents/config.py port，语义逐字保持）────────

_cached_custom_agents: dict[str, dict] | None = None
# 缓存键：(cwd, project_trusted)。任一变化即作废缓存——防止长驻进程切 cwd / 信任态后
# 仍复用旧的「已信任项目 agent」（fail-closed，不依赖显式 reset_agent_cache）。
_cached_agents_key: tuple | None = None


def discover_custom_agents() -> dict[str, dict]:
    """发现自定义 agent 定义（解析后的 .md frontmatter dict，按名归并）。

    Merge-by-name precedence（low → high，later wins）：
      1. 用户级 ~/.agents/agents          （vendor-neutral 通用约定，最低）
      2. 用户级 data_dir()/agents         （~/.nanocode/agents）
      3. 项目级 <cwd>/.agents/agents       （通用约定）
      4. 项目级 project_config_dir()/agents（<cwd>/.nanocode/agents，最高）

    TRUST GATE（P4）：USER 级永远加载；PROJECT 级只在工作区受信任时加载——非交互/未信任
    运行绝不静默加载项目本地 agent 定义（它们可声明 system prompt + 宽工具集）。信任判定
    在发现时现读 trust.is_trusted(cwd)，缓存按 (cwd, trusted) 键控，态变即重判。
    """
    global _cached_custom_agents, _cached_agents_key

    trusted = _project_agents_trusted()
    key = (str(Path.cwd()), bool(trusted))
    if _cached_custom_agents is not None and _cached_agents_key == key:
        return _cached_custom_agents

    agents: dict[str, dict] = {}
    _load_agents_from_dir(Path.home() / ".agents" / "agents", agents)
    _load_agents_from_dir(data_dir() / "agents", agents)
    if trusted:
        _load_agents_from_dir(Path.cwd() / ".agents" / "agents", agents)
        _load_agents_from_dir(project_config_dir() / "agents", agents)

    _cached_custom_agents = agents
    _cached_agents_key = key
    return agents


# 私有别名（原 config._discover_custom_agents；测试/内部沿用下划线名）。
_discover_custom_agents = discover_custom_agents


def _project_agents_trusted() -> bool:
    """项目级 agent 定义是否可加载：当前工作区受信任则 True。

    失败保护：trust 判定抛错时 fail-closed（不加载项目 agent），绝不因信任层故障
    而静默加载不受信目录。委托给独立 impl 便于测试既能整体 stub 本闸、又能验证它
    真正委托 trust.is_trusted。"""
    return _project_agents_trusted_impl()


def _project_agents_trusted_impl() -> bool:
    from ..trust import is_trusted
    try:
        return is_trusted(Path.cwd())
    except Exception:
        return False


def _load_agents_from_dir(directory: Path, agents: dict[str, dict]) -> None:
    if not directory.is_dir():
        return
    for entry in sorted(directory.iterdir()):
        if not entry.suffix == ".md":
            continue
        try:
            raw = entry.read_text()
            result = parse_frontmatter(raw)
            meta = result.meta
            name = meta.get("name") or entry.stem
            # 保留类型不可被 .md 覆盖（curator 等宿主调度类型）。
            if name in RESERVED_AGENT_TYPES:
                continue
            # 'tools' 是 'allowed-tools' 的别名；两者同时存在则并集。
            allowed = as_list(meta.get("allowed-tools")) or []
            tools_alias = as_list(meta.get("tools")) or []
            # 并集（保序去重）。
            seen: set[str] = set()
            allowed_tools: list[str] = []
            for t in (*allowed, *tools_alias):
                if t not in seen:
                    seen.add(t)
                    allowed_tools.append(t)
            disallowed_tools = as_list(meta.get("disallowed-tools")) or []
            max_turns = _as_int(meta.get("max-turns"))
            timeout_ms = _as_int(meta.get("timeout-ms"))
            agents[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "allowed_tools": allowed_tools,
                "disallowed_tools": disallowed_tools,
                "system_prompt": result.body,
                "model": meta.get("model") or None,
                "extends": (str(meta["extends"]).strip() or None) if meta.get("extends") else None,
                "source": str(entry),
                "max_turns": max_turns,
                "timeout_ms": timeout_ms,
            }
        except Exception:
            pass


def _as_int(value) -> int | None:
    """frontmatter 整数字段归一：非法/空 -> None（绝不抛）。"""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def reset_agent_cache() -> None:
    global _cached_custom_agents, _cached_agents_key
    _cached_custom_agents = None
    _cached_agents_key = None


# ─── Effective resolution（extends 收窄代数，自 config._resolve_effective port）──


def _resolve_effective(agent_type: str, _depth: int = 0,
                       _seen: tuple[str, ...] = ()) -> dict:
    """递归解析一个类型的有效配置（含 extends 收窄）。

    返回 {system_prompt, model, description, allowed_names (set|None), disallowed_names (set)}。
    - allowed_names=None 表示"无 allow-list"（除 agent 外全部工具）。
    - 子只能收窄基：allowed = base ∩ child（intersect_allow）；disallowed = base ∪ child。

    环/缺失基/超深度 -> 忽略 extends（不抛）并 **FAIL-CLOSED**（降级只读最小集，宁紧勿松）。
    """
    # 内置基类型（无 extends）。
    if agent_type in ("explore", "plan"):
        prompt = EXPLORE_PROMPT if agent_type == "explore" else PLAN_PROMPT
        return {
            "system_prompt": prompt,
            "model": None,
            "description": "",
            "allowed_names": set(READ_ONLY_TOOLS),
            "disallowed_names": set(),
        }
    if agent_type in ("general", "coder"):
        return {
            "system_prompt": GENERAL_PROMPT,
            "model": None,
            "description": "",
            "allowed_names": None,  # 全部（除 agent）
            "disallowed_names": set(),
        }

    custom = discover_custom_agents().get(agent_type)
    if not custom:
        # 未知类型回退 general 语义。
        return {
            "system_prompt": GENERAL_PROMPT,
            "model": None,
            "description": "",
            "allowed_names": None,
            "disallowed_names": set(),
        }

    # 自定义类型自身的 allow/deny（None = 无 allow-list）。
    self_allowed: set[str] | None = set(custom["allowed_tools"]) if custom["allowed_tools"] else None
    self_disallowed: set[str] = set(custom["disallowed_tools"])

    base = None
    extends = custom.get("extends")
    extends_failed = False
    if extends:
        # 防环 + 防超深度 + 防自指 + 不可 extends 保留类型。
        if (extends in _seen or extends == agent_type
                or _depth >= _MAX_EXTENDS_DEPTH
                or extends in RESERVED_AGENT_TYPES):
            # warning（comment-style，不抛）：忽略此 extends，并 fail-closed。
            print(f"# [agents] ignoring extends='{extends}' for '{agent_type}' "
                  f"(cycle / missing base / max-depth / reserved)", file=sys.stderr, flush=True)
            base = None
            extends_failed = True
        elif extends not in _BUILTIN_BASE_TYPES and extends not in discover_custom_agents():
            print(f"# [agents] ignoring extends='{extends}' for '{agent_type}' "
                  f"(unknown base)", file=sys.stderr, flush=True)
            base = None
            extends_failed = True
        else:
            base = _resolve_effective(extends, _depth + 1, (*_seen, agent_type))

    if base is None:
        # extends 显式声明但解析失败（环/缺失/超深/保留）：FAIL-CLOSED。
        # 不能回退到 child 自己的 allow-list（否则 `extends: explroe` 拼错 + 宽
        # allowed-tools 会静默提权）。降级为只读最小集，宁紧勿松。
        if extends_failed:
            eff_allowed = intersect_allow(set(READ_ONLY_TOOLS), self_allowed)
            eff_disallowed = self_disallowed
        else:
            eff_allowed = self_allowed
            eff_disallowed = self_disallowed
        model = custom.get("model")
        description = custom.get("description", "")
        body = custom["system_prompt"]
    else:
        # Tools: 子只能收窄。allowed = base ∩ child；disallowed = base ∪ child。
        eff_allowed = intersect_allow(base["allowed_names"], self_allowed)
        eff_disallowed = set(base["disallowed_names"]) | self_disallowed
        # Scalars：child 覆盖 base（child 设了才覆盖）。
        model = custom.get("model") or base.get("model")
        description = custom.get("description") or base.get("description", "")
        # system_prompt：child body 非空则替换，否则继承 base。
        body = custom["system_prompt"] if (custom["system_prompt"] or "").strip() else base["system_prompt"]

    return {
        "system_prompt": body,
        "model": model,
        "description": description,
        "allowed_names": eff_allowed,
        "disallowed_names": eff_disallowed,
    }


# ─── typed profile（dict API 的替代，docs/16 #7）──────────────────────────────


def build_profile(agent_type: str) -> AgentProfile:
    """解析一个 agent type 为 typed AgentProfile（subagents/config dict API 的替代）。

    保留类型（memory curators）先于 custom 发现匹配（.nanocode/agents 同名 .md 不能覆盖）：
    mode="system"、tools_allow=set()（显式空 allow-list，恒无工具）、hidden=True。

    其余：tools_allow/tools_deny 是 extends 收窄后的**原始**集合（allow=None = 无 allow-list）；
    有效工具集经 `profile.effective_tool_names(universe)` 计算（allow ∩ → deny − → 剔 agent），
    与原 _filter_tools 逐字节等价——P4 call-time allowlist 从**实际**有效集派生。
    """
    if agent_type == MEMORY_CURATOR_TYPE:
        from ..memory.maintenance import CURATOR_CONSOLIDATION_PROMPT
        return AgentProfile(
            name=agent_type, mode="system", prompt=CURATOR_CONSOLIDATION_PROMPT,
            tools_allow=set(), tools_deny=set(),
            permission=PermissionProfile(), context=ContextProfile(),
            isolation=IsolationPolicy(own_session=True, can_spawn=False), hidden=True)
    if agent_type == MEMORY_EVAL_CURATOR_TYPE:
        return AgentProfile(
            name=agent_type, mode="system", prompt=CURATOR_EVAL_PROMPT,
            tools_allow=set(), tools_deny=set(),
            permission=PermissionProfile(), context=ContextProfile(),
            isolation=IsolationPolicy(own_session=True, can_spawn=False), hidden=True)

    eff = _resolve_effective(agent_type)
    custom = discover_custom_agents().get(agent_type)
    return AgentProfile(
        name=agent_type,
        description=eff.get("description", "") or "",
        mode="subagent",
        prompt=eff["system_prompt"],
        model=eff["model"],
        max_turns=custom.get("max_turns") if custom else None,
        timeout_ms=custom.get("timeout_ms") if custom else None,
        tools_allow=(set(eff["allowed_names"]) if eff["allowed_names"] is not None else None),
        tools_deny=set(eff["disallowed_names"]),
        permission=PermissionProfile(),               # spawn 时从父派生（agents.permissions）
        context=ContextProfile(),
        isolation=IsolationPolicy(own_session=True, can_spawn=False),
        source=custom.get("source") if custom else None,
    )


def effective_tools(profile: AgentProfile) -> list:
    """profile 在全量工具表上的有效 ToolDef 列表（原 _filter_tools 的 typed 等价物）。

    顺序保持 tool_definitions 原序；allow ∩ → deny −（deny 胜出）→ 永剔 'agent'
    （除非 isolation.can_spawn）。"""
    from ..tools import tool_definitions
    names = profile.effective_tool_names({t["name"] for t in tool_definitions})
    return [t for t in tool_definitions if t["name"] in names]


def list_spawnable_profiles() -> list[AgentProfile]:
    """可经 agent 工具 spawn 的 profile（explore/plan/general + 自定义;剔保留 system 类型）。"""
    return [build_profile(t["name"]) for t in get_available_agent_types()]


# ─── Available agent types（显示面，自 config port）───────────────────────────


def get_available_agent_types() -> list[dict[str, str]]:
    types = [
        {"name": "explore", "description": "Fast, read-only codebase search and exploration"},
        {"name": "plan", "description": "Read-only analysis with structured implementation plans"},
        {"name": "general", "description": "Full tools for independent tasks"},
    ]
    for name, defn in discover_custom_agents().items():
        if name in RESERVED_AGENT_TYPES:
            continue  # 保留名不向模型暴露为可 spawn 类型
        types.append({"name": name, "description": defn["description"]})
    return types


def build_agent_descriptions() -> str:
    types = get_available_agent_types()
    if len(types) <= 3:
        return ""  # Only built-in types, already in system prompt
    custom = types[3:]
    lines = ["\n# Custom Agent Types", ""]
    for t in custom:
        lines.append(f"- **{t['name']}**: {t['description']}")
    return "\n".join(lines)


# ─── built-in primary / read-only profiles（§10）──────────────────────────────
def build_primary_profile(*, model: str | None = None) -> AgentProfile:
    """主 write-capable primary agent（build）。无 allow-list（全部工具,含 agent 可 spawn 子）。"""
    return AgentProfile(
        name="build", description="Primary write-capable coding agent",
        mode="primary", model=model,
        tools_allow=None, tools_deny=set(),
        permission=PermissionProfile(mode="default"),
        context=ContextProfile(),
        isolation=IsolationPolicy(own_session=True, can_spawn=True),
    )


def build_plan_profile(*, model: str | None = None) -> AgentProfile:
    """plan primary：read-only(write/shell 受限),用于规划。"""
    return AgentProfile(
        name="plan", description="Read-only planning primary",
        mode="primary", model=model,
        tools_allow={"read_file", "list_files", "grep_search", "write_file", "edit_file"},
        tools_deny={"run_shell"},
        permission=PermissionProfile(mode="plan"),
        context=ContextProfile(),
        isolation=IsolationPolicy(own_session=True, can_spawn=True),
    )
