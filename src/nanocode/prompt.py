"""System prompt construction — template loaded from system_prompt.md, variable interpolation, context gathering."""

from __future__ import annotations

import importlib.resources
import os
import platform
import subprocess
import sys
from pathlib import Path

from .memory import build_memory_prompt_section
from .skills.listing import SKILL_PROMPT_GUIDANCE
from .subagents import build_agent_descriptions
from .tools import get_deferred_tool_names

# ─── System prompt template (externalized to system_prompt.md) ──────────────


def _load_template() -> str:
    return importlib.resources.files("nanocode").joinpath("system_prompt.md").read_text(encoding="utf-8")


import re as _re

# ─── @include resolution ─────────────────────────────────────
# Resolves @./path, @~/path, @/path references in NANOCODE.md / AGENTS.md files.

_INCLUDE_RE = _re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$", _re.MULTILINE)
_MAX_INCLUDE_DEPTH = 5


def _resolve_includes(
    content: str,
    base_path: Path,
    visited: set[str] | None = None,
    depth: int = 0,
) -> str:
    if depth >= _MAX_INCLUDE_DEPTH:
        return content
    if visited is None:
        visited = set()

    def _replace(m: _re.Match) -> str:
        raw = m.group(1)
        if raw.startswith("~/"):
            resolved = Path.home() / raw[2:]
        elif raw.startswith("/"):
            resolved = Path(raw)
        else:
            resolved = base_path / raw
        resolved = resolved.resolve()
        key = str(resolved)
        if key in visited:
            return f"<!-- circular: {raw} -->"
        if not resolved.is_file():
            return f"<!-- not found: {raw} -->"
        try:
            visited.add(key)
            included = resolved.read_text()
            return _resolve_includes(included, resolved.parent, visited, depth + 1)
        except Exception:
            return f"<!-- error reading: {raw} -->"

    return _INCLUDE_RE.sub(_replace, content)


def _load_rules_dir(directory: Path) -> str:
    """Load all .md files from .nanocode/rules/ directory."""
    rules_dir = directory / ".nanocode" / "rules"
    if not rules_dir.is_dir():
        return ""
    try:
        files = sorted(f for f in rules_dir.iterdir() if f.suffix == ".md" and f.is_file())
        if not files:
            return ""
        parts: list[str] = []
        for f in files:
            try:
                content = f.read_text()
                content = _resolve_includes(content, rules_dir)
                parts.append(f"<!-- rule: {f.name} -->\n{content}")
            except Exception:
                pass
        return "\n\n## Rules\n" + "\n\n".join(parts) if parts else ""
    except Exception:
        return ""


# Project instruction files read per directory, in collection order (NANOCODE.md first).
_PROJECT_INSTRUCTION_FILES = ("NANOCODE.md", "AGENTS.md")


def load_project_instructions() -> str:
    """Walk up from cwd collecting NANOCODE.md and AGENTS.md per dir, resolving @includes."""
    parts: list[str] = []
    d = Path.cwd().resolve()
    while True:
        # 同一目录内按 NANOCODE.md → AGENTS.md 顺序收集，整块插到最前，
        # 保持「向上 walk、root-most first」且「NANOCODE.md 在 AGENTS.md 之前」。
        here: list[str] = []
        for fname in _PROJECT_INSTRUCTION_FILES:
            f = d / fname
            if f.is_file():
                try:
                    content = f.read_text()
                    content = _resolve_includes(content, d)
                    here.append(content)
                except Exception:
                    pass
        if here:
            parts[0:0] = here
        parent = d.parent
        if parent == d:
            break
        d = parent
    # Load .nanocode/rules/*.md from cwd
    rules = _load_rules_dir(Path.cwd())
    claude_md = ""
    if parts:
        claude_md = "\n\n# Project Instructions (NANOCODE.md / AGENTS.md)\n" + "\n\n---\n\n".join(parts)
    return claude_md + rules


def get_git_context() -> str:
    """Get git branch, recent commits, and status."""
    try:
        opts = {"encoding": "utf-8", "timeout": 3, "capture_output": True}
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], **opts).stdout.strip()
        log = subprocess.run(["git", "log", "--oneline", "-5"], **opts).stdout.strip()
        status = subprocess.run(["git", "status", "--short"], **opts).stdout.strip()
        result = f"\nGit branch: {branch}"
        if log:
            result += f"\nRecent commits:\n{log}"
        if status:
            result += f"\nGit status:\n{status}"
        return result
    except Exception:
        return ""


def build_system_prompt() -> str:
    """Build the full system prompt from system_prompt.md + dynamic context."""
    from datetime import date
    today = date.today().isoformat()
    plat = f"{platform.system()} {platform.machine()}"
    shell = (os.environ.get("ComSpec") or "cmd.exe") if sys.platform == "win32" else os.environ.get("SHELL", "/bin/sh")
    git_context = get_git_context()
    claude_md = load_project_instructions()
    memory_section = build_memory_prompt_section()
    skills_section = SKILL_PROMPT_GUIDANCE
    agent_section = build_agent_descriptions()

    deferred_names = get_deferred_tool_names()
    deferred_section = (
        f"\n\nThe following deferred tools are available via tool_search: {', '.join(deferred_names)}. Use tool_search to fetch their full schemas when needed."
        if deferred_names else ""
    )

    replacements = {
        "{{cwd}}": str(Path.cwd()),
        "{{date}}": today,
        "{{platform}}": plat,
        "{{shell}}": shell,
        "{{git_context}}": git_context,
        "{{claude_md}}": claude_md,
        "{{memory}}": memory_section,
        "{{skills}}": skills_section,
        "{{agents}}": agent_section,
        "{{deferred_tools}}": deferred_section,
    }
    result = _load_template()
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result
