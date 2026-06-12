"""memory-eval-curator 保留类型 —— docs/16 #7：断言面改写为 build_profile。"""

from nanocode.subagents.prompts import MEMORY_EVAL_CURATOR_TYPE, CURATOR_EVAL_PROMPT
from nanocode.agents.registry import (
    RESERVED_AGENT_TYPES, build_profile, effective_tools, get_available_agent_types,
)


def test_eval_curator_type_constant():
    assert MEMORY_EVAL_CURATOR_TYPE == "memory-eval-curator"
    assert "EVAL" in CURATOR_EVAL_PROMPT.upper()
    assert "candidates" in CURATOR_EVAL_PROMPT  # 严格 JSON 形态被提及


def test_eval_curator_profile_no_tools():
    profile = build_profile(MEMORY_EVAL_CURATOR_TYPE)
    assert profile.prompt == CURATOR_EVAL_PROMPT
    assert effective_tools(profile) == []
    assert profile.mode == "system" and profile.hidden


def test_eval_curator_reserved_and_hidden():
    assert MEMORY_EVAL_CURATOR_TYPE in RESERVED_AGENT_TYPES
    names = [t["name"] for t in get_available_agent_types()]
    assert MEMORY_EVAL_CURATOR_TYPE not in names
