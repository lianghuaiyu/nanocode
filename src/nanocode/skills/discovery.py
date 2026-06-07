"""技能发现：扫描 .nanocode/skills/*/SKILL.md，解析 frontmatter 元数据。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..frontmatter import parse_frontmatter, as_list
from ..paths import data_dir, project_config_dir, CONFIG_DIR_NAME
from .hooks import normalize_hooks

# ─── Types ──────────────────────────────────────────────────


@dataclass
class SkillDefinition:
    name: str
    description: str = ""
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    user_invocable: bool = True
    context: str = "inline"  # "inline" or "fork"
    prompt_template: str = ""
    source: str = "project"  # "project" or "user"
    skill_dir: str = ""
    paths: list[str] | None = None  # paths glob：触碰匹配文件后才条件激活
    disable_model_invocation: bool = False  # True 则不进模型清单、模型直接调用被拒
    hooks: dict | None = None  # frontmatter hooks:（pre/post-tool-use），调用 skill 时注册


# ─── Discovery ──────────────────────────────────────────────

_cached_skills: list[SkillDefinition] | None = None
_extra_skill_dirs: set[str] = set()  # 嵌套发现注册的额外 .nanocode/skills 目录


def discover_skills() -> list[SkillDefinition]:
    global _cached_skills
    if _cached_skills is not None:
        return _cached_skills

    skills: dict[str, SkillDefinition] = {}

    # User-level skills (lower priority)
    user_dir = data_dir() / "skills"
    _load_skills_from_dir(user_dir, "user", skills)

    # Project-level skills (higher priority, overwrites)
    project_dir = project_config_dir() / "skills"
    _load_skills_from_dir(project_dir, "project", skills)

    # 嵌套发现注册的额外目录（祖先链上的 .nanocode/skills）
    for extra in sorted(_extra_skill_dirs):
        _load_skills_from_dir(Path(extra), "project", skills)

    _cached_skills = list(skills.values())
    return _cached_skills


def _load_skills_from_dir(
    base_dir: Path, source: str, skills: dict[str, SkillDefinition]
) -> None:
    if not base_dir.is_dir():
        return
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        skill = _parse_skill_file(skill_file, source, str(entry))
        if skill:
            skills[skill.name] = skill


def _parse_skill_file(
    file_path: Path, source: str, skill_dir: str
) -> SkillDefinition | None:
    try:
        raw = file_path.read_text()
        result = parse_frontmatter(raw)
        meta = result.meta

        name = meta.get("name") or file_path.parent.name or "unknown"
        ui = meta.get("user_invocable", meta.get("user-invocable", True))
        user_invocable = (ui.strip().lower() not in ("false", "no", "0", "off")) if isinstance(ui, str) else bool(ui)
        context = "fork" if meta.get("context") == "fork" else "inline"
        allowed_tools = as_list(meta.get("allowed-tools"))
        paths = as_list(meta.get("paths"))
        dmi = meta.get("disable-model-invocation", meta.get("disable_model_invocation", False))
        disable_model_invocation = (dmi.strip().lower() in ("true", "yes", "1", "on")) if isinstance(dmi, str) else bool(dmi)
        hooks = normalize_hooks(meta.get("hooks"))

        return SkillDefinition(
            name=name,
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use") or meta.get("when-to-use"),
            allowed_tools=allowed_tools,
            user_invocable=user_invocable,
            context=context,
            prompt_template=result.body,
            source=source,
            skill_dir=skill_dir,
            paths=paths,
            disable_model_invocation=disable_model_invocation,
            hooks=hooks,
        )
    except Exception:
        return None


def reset_skill_cache() -> None:
    global _cached_skills, _extra_skill_dirs
    _cached_skills = None
    _extra_skill_dirs = set()


def register_nested_skill_dirs(touched: Path, cwd: Path) -> None:
    """沿 touched 祖先链（cwd 子树内，不含 cwd 本身）注册 .nanocode/skills，有新增则清缓存。"""
    global _cached_skills
    try:
        touched = touched.resolve()
        cwd = cwd.resolve()
    except OSError:
        return
    if touched != cwd and cwd not in touched.parents:
        return  # 限 cwd 子树，不越界
    added = False
    d = touched.parent
    while True:
        sk = d / CONFIG_DIR_NAME / "skills"
        if d != cwd and sk.is_dir() and str(sk) not in _extra_skill_dirs:
            _extra_skill_dirs.add(str(sk))
            added = True
        if d == cwd or d.parent == d:
            break
        d = d.parent
    if added:
        _cached_skills = None


# ─── Path 条件激活 ──────────────────────────────────────────


def _glob_match(path: str, pattern: str) -> bool:
    """单个 glob 匹配：优先 PurePosixPath.full_match（支持 **），失败回退 fnmatch。"""
    from pathlib import PurePosixPath

    try:
        if PurePosixPath(path).full_match(pattern):  # py3.13+，支持 **
            return True
    except (AttributeError, ValueError):
        pass
    import fnmatch

    return fnmatch.fnmatch(path, pattern)


def path_activates_skill(touched: Path, skill: SkillDefinition, cwd: Path) -> bool:
    """skill.paths 任一 glob 命中 touched（相对 cwd 路径或 basename）则激活。"""
    if not skill.paths:
        return False
    try:
        rel = touched.resolve().relative_to(cwd.resolve()).as_posix()
    except (ValueError, OSError):
        rel = touched.name
    base = touched.name
    return any(_glob_match(rel, p) or _glob_match(base, p) for p in skill.paths)
