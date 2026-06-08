"""子 Agent 配置：内置/自定义类型发现、按类型解析系统提示词与可用工具。"""

from __future__ import annotations

import sys
from pathlib import Path

from ..frontmatter import parse_frontmatter, as_list
from ..paths import data_dir, project_config_dir
from ..tools import tool_definitions, ToolDef
from ..memory.maintenance import CURATOR_CONSOLIDATION_PROMPT
from .prompts import (
    EXPLORE_PROMPT, PLAN_PROMPT, GENERAL_PROMPT,
    MEMORY_CURATOR_TYPE, MEMORY_EVAL_CURATOR_TYPE, CURATOR_EVAL_PROMPT,
)

# ─── Read-only tools (for explore and plan agents) ──────────

READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search"}

# 'agent' 元工具永远从任何子 agent 工具集中剔除（子不能 spawn 孙）。
_AGENT_TOOL = "agent"

# extends 解析的最大深度（防御环/恶意深链）。
_MAX_EXTENDS_DEPTH = 5

# 内置基类型（extends 可指向它们）。
_BUILTIN_BASE_TYPES = {"explore", "plan", "general", "coder"}

# ─── Reserved built-in agent types (custom .nanocode/agents 不可覆盖) ──
# 这些类型由宿主特殊调度（如记忆巩固 curator），不能被项目/用户级 .md 覆盖，
# 也不向模型暴露为可 spawn 的 agent type（get_available_agent_types 过滤）。
RESERVED_AGENT_TYPES = {MEMORY_CURATOR_TYPE, MEMORY_EVAL_CURATOR_TYPE}

# ─── Custom agent discovery ─────────────────────────────────

_cached_custom_agents: dict[str, dict] | None = None
# 缓存键：(cwd, project_trusted)。任一变化即作废缓存——防止长驻进程切 cwd / 信任态后
# 仍复用旧的「已信任项目 agent」（fail-closed，不依赖显式 reset_agent_cache）。
_cached_agents_key: tuple | None = None


def _discover_custom_agents() -> dict[str, dict]:
    global _cached_custom_agents, _cached_agents_key

    trusted = _project_agents_trusted()
    key = (str(Path.cwd()), bool(trusted))
    if _cached_custom_agents is not None and _cached_agents_key == key:
        return _cached_custom_agents

    agents: dict[str, dict] = {}
    # Merge-by-name precedence: 后加载的目录覆盖同名条目（low -> high，later wins）。
    # 顺序（低 -> 高）：
    #   1. 用户级 ~/.agents/agents          （legacy/通用约定，最低）
    #   2. 用户级 data_dir()/agents         （~/.nanocode/agents）
    #   3. 项目级 <cwd>/.agents/agents       （通用约定）
    #   4. 项目级 project_config_dir()/agents（<cwd>/.nanocode/agents，最高）
    # 每个条目都带 'source'（.md 绝对路径），便于排查同名碰撞被谁覆盖。
    #
    # TRUST GATE（P4）：USER 级（~/.nanocode/agents、~/.agents/agents）是用户自己的，
    # 永远加载。PROJECT 级（<cwd>/.nanocode/agents、<cwd>/.agents/agents）只在工作区
    # 受信任时加载——非交互/未信任运行绝不静默加载项目本地 agent 定义（它们可声明
    # system prompt + 宽工具集，等同于让不受信项目注入可执行身份）。信任判定在发现时
    # 现读 trust.is_trusted(cwd)，且缓存按 (cwd, trusted) 键控，态变即重判。
    _load_agents_from_dir(Path.home() / ".agents" / "agents", agents)
    _load_agents_from_dir(data_dir() / "agents", agents)
    if trusted:
        _load_agents_from_dir(Path.cwd() / ".agents" / "agents", agents)
        _load_agents_from_dir(project_config_dir() / "agents", agents)

    _cached_custom_agents = agents
    _cached_agents_key = key
    return agents


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


# ─── Effective toolset resolution (with extends) ────────────


def _resolve_effective(agent_type: str, _depth: int = 0,
                       _seen: tuple[str, ...] = ()) -> dict:
    """递归解析一个类型的有效配置（含 extends 收窄）。

    返回 {system_prompt, model, description, allowed_names (set|None), disallowed_names (set)}。
    - allowed_names=None 表示"无 allow-list"（除 agent 外全部工具）。
    - 子只能收窄基：allowed = base ∩ child；disallowed = base ∪ child。

    环/缺失基/超深度 -> 忽略 extends（不抛），相当于该层无基。
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

    custom = _discover_custom_agents().get(agent_type)
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
            print(f"# [subagents] ignoring extends='{extends}' for '{agent_type}' "
                  f"(cycle / missing base / max-depth / reserved)", file=sys.stderr, flush=True)
            base = None
            extends_failed = True
        elif extends not in _BUILTIN_BASE_TYPES and extends not in _discover_custom_agents():
            print(f"# [subagents] ignoring extends='{extends}' for '{agent_type}' "
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
            eff_allowed = _intersect_allowed(set(READ_ONLY_TOOLS), self_allowed)
            eff_disallowed = self_disallowed
        else:
            eff_allowed = self_allowed
            eff_disallowed = self_disallowed
        model = custom.get("model")
        description = custom.get("description", "")
        body = custom["system_prompt"]
    else:
        # Tools: 子只能收窄。allowed = base ∩ child；disallowed = base ∪ child。
        eff_allowed = _intersect_allowed(base["allowed_names"], self_allowed)
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


def _intersect_allowed(base: set[str] | None, child: set[str] | None) -> set[str] | None:
    """allow-list 交集语义：None = 无约束（全部）。

    - base=None, child=None -> None（都不约束）
    - base=None, child=X     -> X（child 收窄）
    - base=Y, child=None     -> Y（base 已约束，child 不放宽）
    - base=Y, child=X        -> Y ∩ X（双重收窄；子绝不会获得 base 没有的工具）
    """
    if base is None and child is None:
        return None
    if base is None:
        return set(child)
    if child is None:
        return set(base)
    return set(base) & set(child)


def _filter_tools(allowed_names: set[str] | None, disallowed_names: set[str]) -> list[ToolDef]:
    """按 allow/deny 计算有效 ToolDef 列表，并永远剔除 'agent'。

    规则：start = allow-list 过滤（无 allow-list 则全部）；再减 disallowed（deny 胜出）；
    最后剔除 'agent'。
    """
    if allowed_names is None:
        names = {t["name"] for t in tool_definitions}
    else:
        names = set(allowed_names)
    names -= set(disallowed_names)   # disallowed 胜出
    names.discard(_AGENT_TOOL)       # 子不能 spawn 孙
    return [t for t in tool_definitions if t["name"] in names]


# ─── Main config function ───────────────────────────────────


def get_sub_agent_config(agent_type: str) -> dict:
    """Return config for the given agent type.

    Backward-compatible shape: always returns at least {system_prompt, tools}.
    Additional keys (non-breaking): 'model', 'source', 'allowed_names',
    'disallowed_names', 'max_turns', 'timeout_ms', 'extends'.

    'allowed_names' is the EFFECTIVE allowed tool-NAME set (or None = unrestricted
    except 'agent'). P4 enforces it at call-time via the ACTUAL effective toolset.

    P4 ENFORCEMENT CONTRACT (now ACTIVE): the engine enforces against the ACTUAL
    effective toolset names ({t['name'] for t in cfg['tools']}), NOT 'allowed_names'
    alone — 'allowed_names' may be None (unrestricted-except-deny) while
    'disallowed_names' still removes tools, so allow-only enforcement would re-permit
    denied tools. _build_sub_agent derives allowed_tool_names from cfg['tools'] (agent
    stripped, disallowed removed) and the sub-agent fail-closes any out-of-set real tool.
    """
    # 保留类型先于 custom 发现匹配：.nanocode/agents 同名 .md 不能覆盖。
    if agent_type == MEMORY_CURATOR_TYPE:
        return {"system_prompt": CURATOR_CONSOLIDATION_PROMPT, "tools": [],
                "model": None, "source": None, "allowed_names": set(),
                "disallowed_names": set(), "max_turns": None, "timeout_ms": None,
                "extends": None}
    if agent_type == MEMORY_EVAL_CURATOR_TYPE:
        return {"system_prompt": CURATOR_EVAL_PROMPT, "tools": [],
                "model": None, "source": None, "allowed_names": set(),
                "disallowed_names": set(), "max_turns": None, "timeout_ms": None,
                "extends": None}

    eff = _resolve_effective(agent_type)
    tools = _filter_tools(eff["allowed_names"], eff["disallowed_names"])

    custom = _discover_custom_agents().get(agent_type)
    source = custom.get("source") if custom else None
    max_turns = custom.get("max_turns") if custom else None
    timeout_ms = custom.get("timeout_ms") if custom else None
    extends = custom.get("extends") if custom else None

    # 有效 allowed-name 集（供 /agents show 与 P4 用）：把 deny 与 agent 剔除后的真实名集。
    effective_allowed_names = {t["name"] for t in tools}

    return {
        "system_prompt": eff["system_prompt"],
        "tools": tools,
        "model": eff["model"],
        "source": source,
        "allowed_names": effective_allowed_names if eff["allowed_names"] is not None else None,
        "disallowed_names": set(eff["disallowed_names"]),
        "max_turns": max_turns,
        "timeout_ms": timeout_ms,
        "extends": extends,
    }


# ─── Available agent types (for system prompt) ──────────────


def get_available_agent_types() -> list[dict[str, str]]:
    types = [
        {"name": "explore", "description": "Fast, read-only codebase search and exploration"},
        {"name": "plan", "description": "Read-only analysis with structured implementation plans"},
        {"name": "general", "description": "Full tools for independent tasks"},
    ]
    for name, defn in _discover_custom_agents().items():
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


def reset_agent_cache() -> None:
    global _cached_custom_agents, _cached_agents_key
    _cached_custom_agents = None
    _cached_agents_key = None
