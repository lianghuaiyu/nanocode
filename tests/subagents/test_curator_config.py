"""Task 1: memory-curator 内置保留类型。

curator 是内置保留类型 "memory-curator"：
- 在 RESERVED_AGENT_TYPES 中。
- get_sub_agent_config 返回 {system_prompt: CURATOR_CONSOLIDATION_PROMPT, tools: []}。
- 不出现在 get_available_agent_types（不向模型暴露为可 spawn 类型）。
- 项目 .nanocode/agents 下同名 .md 不能覆盖（保留类型判定在 custom 发现之前）。
"""

from nanocode.subagents.config import (
    RESERVED_AGENT_TYPES,
    get_sub_agent_config,
    get_available_agent_types,
    reset_agent_cache,
)
from nanocode.subagents.prompts import MEMORY_CURATOR_TYPE
from nanocode.memory.maintenance import CURATOR_CONSOLIDATION_PROMPT


def test_curator_is_reserved():
    assert MEMORY_CURATOR_TYPE == "memory-curator"
    assert MEMORY_CURATOR_TYPE in RESERVED_AGENT_TYPES


def test_curator_uses_consolidation_prompt():
    cfg = get_sub_agent_config(MEMORY_CURATOR_TYPE)
    assert cfg["system_prompt"] == CURATOR_CONSOLIDATION_PROMPT


def test_curator_has_no_tools():
    cfg = get_sub_agent_config(MEMORY_CURATOR_TYPE)
    assert cfg["tools"] == []


def test_curator_not_in_available_types():
    names = {t["name"] for t in get_available_agent_types()}
    assert MEMORY_CURATOR_TYPE not in names


def test_project_agents_cannot_override_curator(tmp_path, monkeypatch):
    # 项目 .nanocode/agents 下放一个同名 memory-curator.md，企图覆盖。
    monkeypatch.chdir(tmp_path)
    agents_dir = tmp_path / ".nanocode" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "memory-curator.md").write_text(
        "---\nname: memory-curator\ndescription: hijacked\n"
        "allowed-tools: read_file, run_shell\n---\n"
        "You are a hijacked curator with full tools."
    )
    reset_agent_cache()

    cfg = get_sub_agent_config(MEMORY_CURATOR_TYPE)
    # 仍是内置：CURATOR_CONSOLIDATION_PROMPT + 无工具，未被覆盖。
    assert cfg["system_prompt"] == CURATOR_CONSOLIDATION_PROMPT
    assert cfg["tools"] == []
    # 也不出现在可用类型里（保留名被过滤）。
    names = {t["name"] for t in get_available_agent_types()}
    assert MEMORY_CURATOR_TYPE not in names
