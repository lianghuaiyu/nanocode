from nanocode.prompt import (
    _resolve_includes,
    build_system_prompt,
    load_project_instructions,
)


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


def test_load_project_instructions_reads_nanocode_and_agents(tmp_path, monkeypatch):
    """每层目录同时收集 NANOCODE.md 和 AGENTS.md（NANOCODE.md 在前）。"""
    (tmp_path / "NANOCODE.md").write_text("NANO_INSTRUCTIONS")
    (tmp_path / "AGENTS.md").write_text("AGENTS_INSTRUCTIONS")
    monkeypatch.chdir(tmp_path)
    out = load_project_instructions()
    assert "# Project Instructions (NANOCODE.md / AGENTS.md)" in out
    assert "NANO_INSTRUCTIONS" in out
    assert "AGENTS_INSTRUCTIONS" in out
    # NANOCODE.md collected before AGENTS.md within the same directory.
    assert out.index("NANO_INSTRUCTIONS") < out.index("AGENTS_INSTRUCTIONS")


def test_load_project_instructions_agents_only(tmp_path, monkeypatch):
    """只有 AGENTS.md 时也应被读取（跨工具互通）。"""
    (tmp_path / "AGENTS.md").write_text("ONLY_AGENTS")
    monkeypatch.chdir(tmp_path)
    out = load_project_instructions()
    assert "ONLY_AGENTS" in out


def test_load_project_instructions_ignores_claude_md(tmp_path, monkeypatch):
    """旧的 CLAUDE.md 不再被读取（A2 直接换名，无双读）。"""
    (tmp_path / "CLAUDE.md").write_text("LEGACY_CLAUDE")
    monkeypatch.chdir(tmp_path)
    out = load_project_instructions()
    assert "LEGACY_CLAUDE" not in out
