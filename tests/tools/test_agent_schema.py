"""Task 2: agent 工具 schema 扩展 —— coder enum + resume + run_in_background。

docs/26 G7：模块级 `SCHEMA` 现在是 slim 的 `BASE_SCHEMA`（单 spawn 面，无编排词汇）；
steps/tasks/accept/plan_fanout 移到 `ORCHESTRATION_SCHEMA`，仅编排扩展激活时由 facade overlay。"""

from nanocode.tools.agent import BASE_SCHEMA, ORCHESTRATION_SCHEMA, SCHEMA


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
    # G7：编排词汇只在 ORCHESTRATION_SCHEMA 出现，不在常驻 slim SCHEMA 里。
    props = ORCHESTRATION_SCHEMA["input_schema"]["properties"]
    for key in ("steps", "tasks"):
        assert props[key]["type"] == "array"
        assert props[key]["items"]["required"] == ["prompt"]
    assert "{previous}" in ORCHESTRATION_SCHEMA["description"]


def test_base_schema_has_no_orchestration_vocab():
    # 常驻 builtin（=SCHEMA=BASE_SCHEMA）不含编排键，description 不提 {previous}/steps。
    assert SCHEMA is BASE_SCHEMA
    props = SCHEMA["input_schema"]["properties"]
    for key in ("steps", "tasks", "accept", "plan_fanout"):
        assert key not in props
    assert "{previous}" not in SCHEMA["description"]
    # 单 spawn 面字段仍在。
    for key in ("description", "prompt", "type", "resume", "run_in_background", "timeout_ms"):
        assert key in props


def test_orchestration_schema_is_base_superset():
    # ORCHESTRATION_SCHEMA = BASE 的全部 spawn 字段 + 4 个编排键（同名 'agent'）。
    base_props = set(BASE_SCHEMA["input_schema"]["properties"])
    orch_props = set(ORCHESTRATION_SCHEMA["input_schema"]["properties"])
    assert base_props <= orch_props
    assert orch_props - base_props == {"steps", "tasks", "accept", "plan_fanout"}
    assert ORCHESTRATION_SCHEMA["name"] == BASE_SCHEMA["name"] == "agent"

