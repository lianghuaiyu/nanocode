"""docs/26 §0.6 策略库：verify.py 单元 —— reviewer 裁决解析 + 轻量 schema 校验。"""
from nanocode.extensions.orchestration.verify import parse_verdict, validate_schema


# ─── parse_verdict ─────────────────────────────────────────────────────────────

def test_parse_verdict_accept():
    assert parse_verdict('{"accept": true, "feedback": "ok"}') == (True, "ok")


def test_parse_verdict_reject():
    assert parse_verdict('{"accept": false, "feedback": "fix it"}') == (False, "fix it")


def test_parse_verdict_extracts_from_prose():
    ok, fb = parse_verdict('My verdict: {"accept": true, "feedback": "great"} — done.')
    assert ok is True and fb == "great"


def test_parse_verdict_unparseable_is_reject():
    ok, fb = parse_verdict("totally not json")
    assert ok is False and "unparseable" in fb


def test_parse_verdict_missing_accept_is_reject():
    ok, fb = parse_verdict('{"feedback": "no verdict key"}')
    assert ok is False and "missing 'accept'" in fb


# ─── validate_schema ───────────────────────────────────────────────────────────

def test_validate_schema_pass():
    schema = {"type": "object", "required": ["a", "b"],
              "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}}
    assert validate_schema('{"a": 1, "b": "x"}', schema) == []


def test_validate_schema_missing_required():
    errs = validate_schema('{"a": 1}', {"type": "object", "required": ["a", "b"]})
    assert any("missing required key 'b'" in e for e in errs)


def test_validate_schema_type_mismatch():
    errs = validate_schema('{"a": "x"}', {"type": "object", "properties": {"a": {"type": "integer"}}})
    assert any("expected type integer" in e for e in errs)


def test_validate_schema_bad_json():
    assert validate_schema("not json", {"type": "object"}) == ["output is not valid JSON: not json"]


def test_validate_schema_array_items():
    errs = validate_schema('[1, "x", 3]', {"type": "array", "items": {"type": "integer"}})
    assert any(e.startswith("[1]") for e in errs)


def test_validate_schema_bool_is_not_integer():
    # bool 是 int 子类——integer 校验须排除 bool。
    errs = validate_schema('{"n": true}', {"type": "object", "properties": {"n": {"type": "integer"}}})
    assert errs


def test_validate_schema_unknown_keyword_ignored():
    # 非完整 JSON Schema：不支持的 type 关键字忽略（不报错）。
    assert validate_schema('{"a": 1}', {"type": "object", "properties": {"a": {"type": "weird"}}}) == []
