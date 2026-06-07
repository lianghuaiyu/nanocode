"""Task 3: coder 类型归一 —— coder 与 general 显式同义。"""

from nanocode.subagents.config import get_sub_agent_config
from nanocode.subagents.prompts import EXPLORE_PROMPT, GENERAL_PROMPT


def test_coder_is_synonym_of_general():
    coder = get_sub_agent_config("coder")
    general = get_sub_agent_config("general")
    assert coder["system_prompt"] == general["system_prompt"]
    coder_names = {t["name"] for t in coder["tools"]}
    general_names = {t["name"] for t in general["tools"]}
    assert coder_names == general_names


def test_coder_uses_general_prompt():
    assert get_sub_agent_config("coder")["system_prompt"] == GENERAL_PROMPT


def test_coder_has_full_tools_minus_agent():
    coder = get_sub_agent_config("coder")
    names = {t["name"] for t in coder["tools"]}
    assert "agent" not in names
    assert "read_file" in names
    assert "run_shell" in names


def test_explore_is_read_only():
    explore = get_sub_agent_config("explore")
    assert explore["system_prompt"] == EXPLORE_PROMPT
    names = {t["name"] for t in explore["tools"]}
    assert names <= {"read_file", "list_files", "grep_search"}
    assert "run_shell" not in names
