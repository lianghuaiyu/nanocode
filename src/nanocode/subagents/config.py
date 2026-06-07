"""子 Agent 配置：内置/自定义类型发现、按类型解析系统提示词与可用工具。"""

from __future__ import annotations

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

# ─── Reserved built-in agent types (custom .nanocode/agents 不可覆盖) ──
# 这些类型由宿主特殊调度（如记忆巩固 curator），不能被项目/用户级 .md 覆盖，
# 也不向模型暴露为可 spawn 的 agent type（get_available_agent_types 过滤）。
RESERVED_AGENT_TYPES = {MEMORY_CURATOR_TYPE, MEMORY_EVAL_CURATOR_TYPE}

# ─── Custom agent discovery ─────────────────────────────────

_cached_custom_agents: dict[str, dict] | None = None


def _discover_custom_agents() -> dict[str, dict]:
    global _cached_custom_agents
    if _cached_custom_agents is not None:
        return _cached_custom_agents

    agents: dict[str, dict] = {}
    # User-level (lower priority)
    _load_agents_from_dir(data_dir() / "agents", agents)
    # Project-level (higher priority, overwrites)
    _load_agents_from_dir(project_config_dir() / "agents", agents)

    _cached_custom_agents = agents
    return agents


def _load_agents_from_dir(directory: Path, agents: dict[str, dict]) -> None:
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if not entry.suffix == ".md":
            continue
        try:
            raw = entry.read_text()
            result = parse_frontmatter(raw)
            meta = result.meta
            name = meta.get("name") or entry.stem
            allowed_tools = as_list(meta.get("allowed-tools"))
            agents[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "allowed_tools": allowed_tools,
                "system_prompt": result.body,
            }
        except Exception:
            pass


# ─── Main config function ───────────────────────────────────


def get_sub_agent_config(agent_type: str) -> dict:
    """Return {system_prompt, tools} for the given agent type."""
    # 保留类型先于 custom 发现匹配：.nanocode/agents 同名 .md 不能覆盖。
    if agent_type == MEMORY_CURATOR_TYPE:
        return {"system_prompt": CURATOR_CONSOLIDATION_PROMPT, "tools": []}
    if agent_type == MEMORY_EVAL_CURATOR_TYPE:
        return {"system_prompt": CURATOR_EVAL_PROMPT, "tools": []}

    custom = _discover_custom_agents().get(agent_type)
    if custom:
        if custom["allowed_tools"]:
            tools = [t for t in tool_definitions if t["name"] in custom["allowed_tools"]]
        else:
            tools = [t for t in tool_definitions if t["name"] != "agent"]
        return {"system_prompt": custom["system_prompt"], "tools": tools}

    read_only = [t for t in tool_definitions if t["name"] in READ_ONLY_TOOLS]
    full_tools = [t for t in tool_definitions if t["name"] != "agent"]

    if agent_type == "explore":
        return {"system_prompt": EXPLORE_PROMPT, "tools": read_only}
    elif agent_type == "plan":
        return {"system_prompt": PLAN_PROMPT, "tools": read_only}
    elif agent_type in ("general", "coder"):  # coder 与 general 显式同义
        return {"system_prompt": GENERAL_PROMPT, "tools": full_tools}
    else:  # 未知类型回退到 general 语义
        return {"system_prompt": GENERAL_PROMPT, "tools": full_tools}


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
    global _cached_custom_agents
    _cached_custom_agents = None
