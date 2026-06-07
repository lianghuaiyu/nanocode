from nanocode.subagents.prompts import MEMORY_EVAL_CURATOR_TYPE, CURATOR_EVAL_PROMPT
from nanocode.subagents.config import (
    get_sub_agent_config, get_available_agent_types, RESERVED_AGENT_TYPES,
)


def test_eval_curator_type_constant():
    assert MEMORY_EVAL_CURATOR_TYPE == "memory-eval-curator"
    assert "EVAL" in CURATOR_EVAL_PROMPT.upper()
    assert "candidates" in CURATOR_EVAL_PROMPT  # 严格 JSON 形态被提及


def test_eval_curator_config_no_tools():
    cfg = get_sub_agent_config(MEMORY_EVAL_CURATOR_TYPE)
    assert cfg["system_prompt"] == CURATOR_EVAL_PROMPT
    assert cfg["tools"] == []


def test_eval_curator_reserved_and_hidden():
    assert MEMORY_EVAL_CURATOR_TYPE in RESERVED_AGENT_TYPES
    names = [t["name"] for t in get_available_agent_types()]
    assert MEMORY_EVAL_CURATOR_TYPE not in names
