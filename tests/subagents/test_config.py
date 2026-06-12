"""coder 类型归一（coder 与 general 显式同义）—— docs/16 #7：断言面改写为 build_profile。"""

from nanocode.agents.registry import build_profile, effective_tools
from nanocode.subagents.prompts import EXPLORE_PROMPT, GENERAL_PROMPT


def _names(agent_type):
    return {t["name"] for t in effective_tools(build_profile(agent_type))}


def test_coder_is_synonym_of_general():
    coder = build_profile("coder")
    general = build_profile("general")
    assert coder.prompt == general.prompt
    assert _names("coder") == _names("general")


def test_coder_uses_general_prompt():
    assert build_profile("coder").prompt == GENERAL_PROMPT


def test_coder_has_full_tools_minus_agent():
    names = _names("coder")
    assert "agent" not in names
    assert "read_file" in names
    assert "run_shell" in names


def test_explore_is_read_only():
    explore = build_profile("explore")
    assert explore.prompt == EXPLORE_PROMPT
    names = _names("explore")
    assert names <= {"read_file", "list_files", "grep_search"}
    assert "run_shell" not in names
