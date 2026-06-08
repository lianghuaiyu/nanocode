"""P3: 子 agent 结构化结果解析器（subagents/result.py）。

覆盖：fenced agent-result JSON 提取 findings；markdown ## Findings/## Summary；
无结构块/坏 JSON 干净回退（首 ~500 字符，findings=[]）；解析器永不抛、对巨输入有界。
"""

from nanocode.subagents.result import parse_structured_result, SUMMARY_FALLBACK_CHARS


def test_fenced_agent_result_extracts_summary_and_findings():
    text = (
        "Here is my work.\n\n"
        "```agent-result\n"
        '{"summary": "Refactored the parser", '
        '"findings": ["bug in tokenizer", "missing test for edge case"]}\n'
        "```\n"
    )
    r = parse_structured_result(text)
    assert r["summary"] == "Refactored the parser"
    assert r["findings"] == ["bug in tokenizer", "missing test for edge case"]


def test_fenced_agent_result_tolerates_extra_info_tokens():
    text = (
        "prose\n```json agent-result\n"
        '{"summary": "ok", "findings": ["a"]}\n```\n'
    )
    r = parse_structured_result(text)
    assert r["summary"] == "ok"
    assert r["findings"] == ["a"]


def test_markdown_sections_extract_findings():
    text = (
        "## Summary\n"
        "Investigated the auth flow.\n\n"
        "## Findings\n"
        "- token never refreshed\n"
        "* logout leaks session\n"
        "1. race in cache\n"
    )
    r = parse_structured_result(text)
    assert "Investigated the auth flow." in r["summary"]
    assert r["findings"] == ["token never refreshed", "logout leaks session", "race in cache"]


def test_markdown_findings_only_summary_falls_back():
    text = "blah blah body\n\n## Findings\n- only a finding\n"
    r = parse_structured_result(text)
    # no ## Summary section → summary falls back to leading text
    assert "blah blah body" in r["summary"]
    assert r["findings"] == ["only a finding"]


def test_no_structure_falls_back_to_first_500_chars():
    text = "X" * 1200
    r = parse_structured_result(text)
    assert r["summary"] == "X" * SUMMARY_FALLBACK_CHARS
    assert r["findings"] == []


def test_garbage_fenced_json_falls_back_cleanly():
    text = "prose body here\n```agent-result\n{not valid json,,,}\n```\n"
    r = parse_structured_result(text)
    # bad JSON → no exception, fall back to first ~500 chars, findings=[]
    assert "prose body here" in r["summary"]
    assert r["findings"] == []


def test_empty_text_does_not_raise():
    r = parse_structured_result("")
    assert r["summary"] == ""
    assert r["findings"] == []


def test_findings_non_string_items_are_dropped():
    text = (
        "```agent-result\n"
        '{"summary": "s", "findings": ["keep", 42, null, {"text": "from dict"}]}\n'
        "```\n"
    )
    r = parse_structured_result(text)
    assert r["findings"] == ["keep", "from dict"]


def test_huge_input_is_bounded_and_returns_quickly():
    # 2MB of noise with a structured block at the very end — parser must only scan the tail.
    text = ("noise line\n" * 200000) + (
        "```agent-result\n"
        '{"summary": "tail summary", "findings": ["f1"]}\n'
        "```\n"
    )
    r = parse_structured_result(text)
    assert r["summary"] == "tail summary"
    assert r["findings"] == ["f1"]


def test_fenced_block_with_empty_summary_uses_fallback():
    text = "real body content\n```agent-result\n{\"findings\": [\"f\"]}\n```\n"
    r = parse_structured_result(text)
    # summary missing in JSON → fall back to leading text; findings still parsed
    assert "real body content" in r["summary"]
    assert r["findings"] == ["f"]
