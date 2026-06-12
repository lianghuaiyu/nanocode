from nanocode.skills.discovery import SkillDefinition
from nanocode.skills.listing import (
    build_skill_listing, skill_listing_delta,
    render_skill_body_message,
)


def _skill(name, desc="", when=None, invocable=True):
    return SkillDefinition(
        name=name, description=desc, when_to_use=when, allowed_tools=None,
        user_invocable=invocable, context="inline", prompt_template="body",
        source="project", skill_dir="/x",
    )


def test_build_listing_basic():
    out = build_skill_listing([_skill("a", "does A"), _skill("b", "does B")])
    assert "**/a**: does A" in out
    assert "**/b**: does B" in out


def test_build_listing_per_item_truncation():
    out = build_skill_listing([_skill("a", "x" * 400)], per_item=250)
    line = [l for l in out.splitlines() if l.startswith("- ")][0]
    assert len(line) < 300 and line.rstrip().endswith("…")


def test_build_listing_names_only_when_over_budget():
    skills = [_skill(f"s{i}", "y" * 100) for i in range(20)]
    out = build_skill_listing(skills, char_budget=200)
    assert "y" * 100 not in out          # 描述被丢弃
    assert "**/s0**" in out               # 名字仍在


def test_build_listing_empty():
    assert build_skill_listing([]) == ""


def test_render_body_message_has_command_name():
    m = render_skill_body_message("commit", "do the commit")
    assert m["role"] == "user"
    assert "<command-name>commit</command-name>" in m["content"]
    assert "do the commit" in m["content"]



def test_skill_listing_delta_diff(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "z1"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: z1\ndescription: zee\n---\nbody")
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    text, new = skill_listing_delta(set())
    assert text is not None and "<system-reminder>" in text and "z1" in text
    assert "z1" in new
    # 已播报后无新增
    text2, new2 = skill_listing_delta(set(new))
    assert text2 is None and new2 == []


def _skill2(name, paths=None, dmi=False):
    from nanocode.skills.discovery import SkillDefinition
    return SkillDefinition(name=name, description="d", when_to_use=None, allowed_tools=None,
                           user_invocable=True, context="inline", prompt_template="b",
                           source="project", skill_dir="/x", paths=paths,
                           disable_model_invocation=dmi)


def test_visible_excludes_disabled():
    from nanocode.skills.listing import visible_model_skills
    vis = visible_model_skills([_skill2("a"), _skill2("b", dmi=True)], set())
    assert [s.name for s in vis] == ["a"]


def test_visible_gates_paths_until_activated():
    from nanocode.skills.listing import visible_model_skills
    skills = [_skill2("plain"), _skill2("gated", paths=["*.py"])]
    assert [s.name for s in visible_model_skills(skills, set())] == ["plain"]
    assert {s.name for s in visible_model_skills(skills, {"gated"})} == {"plain", "gated"}
