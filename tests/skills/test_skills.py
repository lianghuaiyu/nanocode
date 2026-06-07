from pathlib import Path

import pytest
from nanocode.skills import discovery, resolve


@pytest.fixture
def skill_repo(tmp_path, monkeypatch):
    d = tmp_path / ".nanocode" / "skills" / "foo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: foo\ndescription: a foo skill\ncontext: inline\n---\n"
        "Do $ARGUMENTS in ${CLAUDE_SKILL_DIR}"
    )
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    return tmp_path


def test_discover(skill_repo):
    assert any(s.name == "foo" for s in discovery.discover_skills())


def test_resolve_args(skill_repo):
    s = resolve.get_skill_by_name("foo")
    out = resolve.resolve_skill_prompt(s, "BAR")
    assert "Do BAR in" in out
    assert s.skill_dir in out


def test_execute_skill_inline(skill_repo):
    r = resolve.execute_skill("foo", "BAZ")
    assert r["context"] == "inline"
    assert "BAZ" in r["prompt"]


def test_user_invocable_false_disables(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "s1"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s1\nuser_invocable: false\n---\nbody")
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "s1")
    assert s.user_invocable is False


def test_user_invocable_underscore_true_read(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "s2"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s2\nuser_invocable: true\n---\nbody")
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "s2")
    assert s.user_invocable is True


def test_allowed_tools_yaml_list(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "s3"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s3\nallowed-tools:\n  - read_file\n  - run_shell\n---\nb")
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "s3")
    assert s.allowed_tools == ["read_file", "run_shell"]


def test_paths_field_parsed(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "p1"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: p1\npaths:\n  - 'src/**/*.py'\n  - '*.md'\n---\nb")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "p1")
    assert s.paths == ["src/**/*.py", "*.md"]


def test_disable_model_invocation_parsed(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "p2"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: p2\ndisable-model-invocation: true\n---\nb")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "p2")
    assert s.disable_model_invocation is True


def test_defaults_no_paths_not_disabled(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "p3"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: p3\ndescription: d\n---\nb")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "p3")
    assert s.paths is None and s.disable_model_invocation is False


def test_path_activates_skill(tmp_path):
    from nanocode.skills.discovery import path_activates_skill, SkillDefinition
    cwd = tmp_path
    s = SkillDefinition(name="g", description="", paths=["src/**/*.py", "*.md"])
    assert path_activates_skill(cwd / "src" / "a" / "b.py", s, cwd) is True   # ** 跨层
    assert path_activates_skill(cwd / "README.md", s, cwd) is True            # basename
    assert path_activates_skill(cwd / "src" / "a.txt", s, cwd) is False       # 不匹配
    assert path_activates_skill(cwd / "x.py", SkillDefinition(name="n"), cwd) is False  # 无 paths


def test_nested_skill_discovery(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    # cwd 下没有顶层 skill；嵌套子目录里有一个
    nested = tmp_path / "pkg" / "sub" / ".nanocode" / "skills" / "nestedskill"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: nestedskill\ndescription: n\n---\nb")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    assert all(s.name != "nestedskill" for s in discovery.discover_skills())  # 初始不可见
    discovery.register_nested_skill_dirs(tmp_path / "pkg" / "sub" / "f.py", tmp_path)
    assert any(s.name == "nestedskill" for s in discovery.discover_skills())  # 注册后可见


def test_nested_discovery_stays_in_cwd_subtree(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    (tmp_path / "inside").mkdir()
    monkeypatch.chdir(tmp_path / "inside")
    # 触碰 cwd 之外的文件不应注册任何东西（不抛、无新增）
    discovery.reset_skill_cache()
    before = len(discovery.discover_skills())
    discovery.register_nested_skill_dirs(Path("/etc/hosts"), Path.cwd())
    assert len(discovery.discover_skills()) == before


def test_skill_hooks_parsed(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "guard"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: guard\nhooks:\n  post-tool-use:\n"
        "    - matcher: [write_file, edit_file]\n      command: pytest -q\n---\nbody"
    )
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "guard")
    assert s.hooks["post-tool-use"][0]["command"] == "pytest -q"
    assert s.hooks["post-tool-use"][0]["matcher"] == ["write_file", "edit_file"]


def test_skill_no_hooks_is_none(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "plain"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: plain\ndescription: d\n---\nbody")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    s = next(x for x in discovery.discover_skills() if x.name == "plain")
    assert s.hooks is None
