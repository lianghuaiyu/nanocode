from nanocode.frontmatter import parse_frontmatter, format_frontmatter


def test_parse_basic():
    r = parse_frontmatter("---\nname: x\ntype: user\n---\nbody text")
    assert r.meta == {"name": "x", "type": "user"}
    assert r.body == "body text"


def test_parse_no_frontmatter():
    r = parse_frontmatter("just body")
    assert r.meta == {}
    assert r.body == "just body"


def test_parse_unterminated():
    r = parse_frontmatter("---\nname: x\nno end")
    assert r.body == "---\nname: x\nno end"


def test_format_roundtrip():
    text = format_frontmatter({"name": "x", "type": "user"}, "hello")
    r = parse_frontmatter(text)
    assert r.meta["name"] == "x"
    assert r.body == "hello"


def test_yaml_list():
    r = parse_frontmatter("---\nallowed-tools:\n  - read_file\n  - run_shell\n---\nb")
    assert r.meta["allowed-tools"] == ["read_file", "run_shell"]


def test_yaml_bool():
    r = parse_frontmatter("---\nuser_invocable: false\n---\nb")
    assert r.meta["user_invocable"] is False


def test_yaml_nested_metadata():
    r = parse_frontmatter("---\nname: m\nmetadata:\n  type: project\n---\nbody")
    assert r.meta["metadata"]["type"] == "project"
    assert r.meta["name"] == "m"


def test_yaml_quoted_description_stripped():
    r = parse_frontmatter('---\ndescription: "hi there"\n---\nb')
    assert r.meta["description"] == "hi there"


def test_lenient_glob_value_autoquote():
    # 值以 * 开头会让 strict YAML 报错；auto-quote 兜底应救回
    r = parse_frontmatter("---\npaths: *.py\n---\nb")
    assert r.meta["paths"] == "*.py"


def test_corrupt_yaml_falls_back_not_raises():
    # 怪异缩进/不完整：不得抛异常，回退后至少不崩
    r = parse_frontmatter("---\nname: x\n  : broken\n---\nbody")
    assert isinstance(r.meta, dict)
    assert r.body == "body"


def test_as_list():
    from nanocode.frontmatter import as_list
    assert as_list(["a", "b"]) == ["a", "b"]
    assert as_list("a, b ,c") == ["a", "b", "c"]
    assert as_list("[a, b]") == ["a", "b"]
    assert as_list(None) is None
    assert as_list("") is None
