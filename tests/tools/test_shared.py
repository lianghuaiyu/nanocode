from nanocode.tools import shared


def test_normalize_quotes():
    assert shared._normalize_quotes("‘x’") == "'x'"
    assert shared._normalize_quotes("“hi”") == '"hi"'


def test_find_actual_string_exact():
    assert shared._find_actual_string("abc", "b") == "b"


def test_find_actual_string_via_quotes():
    f = 'say “hi”'          # 文件里是花引号
    assert shared._find_actual_string(f, '"hi"') is not None


def test_generate_diff():
    d = shared._generate_diff("foo\nbar", "foo", "baz")
    assert d.startswith("@@")
    assert "- foo" in d and "+ baz" in d


def test_truncate():
    s = "a" * (shared.MAX_RESULT_CHARS + 100)
    out = shared._truncate_result(s)
    assert "truncated" in out and len(out) < len(s)
