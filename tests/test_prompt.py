from nanocode.prompt import _resolve_includes, build_system_prompt


def test_resolve_includes_relative(tmp_path):
    (tmp_path / "inc.md").write_text("INCLUDED")
    out = _resolve_includes("before\n@./inc.md\nafter", tmp_path)
    assert "INCLUDED" in out
    assert "@./inc.md" not in out


def test_resolve_includes_missing(tmp_path):
    out = _resolve_includes("@./nope.md", tmp_path)
    assert "not found" in out


def test_resolve_includes_circular(tmp_path):
    (tmp_path / "a.md").write_text("@./b.md")
    (tmp_path / "b.md").write_text("@./a.md")
    out = _resolve_includes("@./a.md", tmp_path)  # 必须不死循环
    assert "circular" in out


def test_build_system_prompt_substitutes():
    s = build_system_prompt()
    assert "{{cwd}}" not in s
    assert "You are nanocode" in s
