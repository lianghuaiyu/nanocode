"""Task 2: agent 工具 schema 扩展 —— coder enum + resume + run_in_background。"""

from nanocode.tools.agent import SCHEMA


def test_schema_name():
    assert SCHEMA["name"] == "agent"


def test_type_enum_includes_coder():
    enum = SCHEMA["input_schema"]["properties"]["type"]["enum"]
    assert set(enum) == {"explore", "plan", "general", "coder"}


def test_resume_is_optional_string():
    props = SCHEMA["input_schema"]["properties"]
    assert "resume" in props
    assert props["resume"]["type"] == "string"


def test_run_in_background_is_optional_boolean():
    props = SCHEMA["input_schema"]["properties"]
    assert "run_in_background" in props
    assert props["run_in_background"]["type"] == "boolean"


def test_required_unchanged():
    assert SCHEMA["input_schema"]["required"] == ["description", "prompt"]
