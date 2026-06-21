"""Task 2: agent 工具 schema 扩展 —— coder enum + resume + run_in_background。"""

from nanocode.tools.agent import SCHEMA


def test_schema_name():
    assert SCHEMA["name"] == "agent"


def test_type_is_free_string_not_enum():
    # P1 fix (Codex review): the restrictive enum was removed so discovered custom
    # agent types are selectable by schema-honoring backends. 'type' is now a free
    # string whose description documents the built-ins + custom types.
    t = SCHEMA["input_schema"]["properties"]["type"]
    assert t["type"] == "string"
    assert "enum" not in t
    desc = t["description"].lower()
    for builtin in ("explore", "plan", "general", "coder"):
        assert builtin in desc
    assert "custom" in desc


def test_resume_is_optional_string():
    props = SCHEMA["input_schema"]["properties"]
    assert "resume" in props
    assert props["resume"]["type"] == "string"


def test_run_in_background_is_optional_boolean():
    props = SCHEMA["input_schema"]["properties"]
    assert "run_in_background" in props
    assert props["run_in_background"]["type"] == "boolean"


def test_no_global_required_fields():
    # resume/steer/run modes have different required inputs; runtime validates the
    # selected mode instead of making description globally mandatory.
    assert SCHEMA["input_schema"]["required"] == []


def test_steps_and_tasks_orchestration_arrays():
    props = SCHEMA["input_schema"]["properties"]
    for key in ("steps", "tasks"):
        assert props[key]["type"] == "array"
        assert props[key]["items"]["required"] == ["prompt"]
    assert "{previous}" in SCHEMA["description"]
