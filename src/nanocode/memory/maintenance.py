"""Memory maintenance backend: consolidation (Auto-Dream) + optimization (EvolveMem).

This module implements the deterministic Python backend that:
- Accepts consolidation plans from the curator sub-agent (JSON proposals)
- Validates, backs up, and atomically applies changes
- Archives deleted/merged memories (never hard-deletes)
- Manages the optimization config lifecycle

Key principle: the curator sub-agent PROPOSES (strict JSON); this module EXECUTES.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import project_memory_dir


# ─── Data Directory Layout ──────────────────────────────────

def _archive_dir() -> Path:
    d = project_memory_dir() / "_archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_dir() -> Path:
    d = project_memory_dir() / "_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _simplemem_dir() -> Path:
    """SimpleMem eval store directory (the eval/ tree lives under here)."""
    mem_dir = project_memory_dir()
    d = mem_dir.parent / "simplemem"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Consolidation Plan Model ──────────────────────────────

VALID_ACTIONS = ("delete", "merge", "rewrite", "normalize_date")


@dataclass
class ConsolidationAction:
    """A single proposed action from the curator."""
    action: str  # one of VALID_ACTIONS
    targets: list[str]  # filenames to operate on
    reason: str = ""
    new_content: str | None = None  # for rewrite/merge: the replacement content
    new_filename: str | None = None  # for merge: the output filename


@dataclass
class ConsolidationPlan:
    """Full consolidation plan from the curator sub-agent."""
    actions: list[ConsolidationAction] = field(default_factory=list)
    summary: str = ""


@dataclass
class ConsolidationResult:
    """Result of applying a consolidation plan."""
    archived: int = 0
    rewritten: int = 0
    merged: int = 0
    normalized: int = 0
    errors: list[str] = field(default_factory=list)
    backup_id: str = ""

    @property
    def total_actions(self) -> int:
        return self.archived + self.rewritten + self.merged + self.normalized

    def summary_line(self) -> str:
        parts = []
        if self.archived:
            parts.append(f"archived {self.archived}")
        if self.rewritten:
            parts.append(f"rewritten {self.rewritten}")
        if self.merged:
            parts.append(f"merged {self.merged}")
        if self.normalized:
            parts.append(f"date-normalized {self.normalized}")
        if self.errors:
            parts.append(f"errors {len(self.errors)}")
        return f"Consolidation: {', '.join(parts) or 'no changes'} (backup={self.backup_id})"


# ─── Plan Parsing & Validation ──────────────────────────────


def extract_json_object(text: str) -> str:
    """从 LLM 输出里提取 JSON 对象字符串。

    LLM 常把 JSON 包在  ...  代码围栏里，或前后带说明文字。
    策略：找第一个 '{' 到与之平衡的 '}'（计入字符串内的转义/引号），返回该子串。
    找不到则原样返回（交给 json.loads 抛错走降级）。
    """
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return text[start:]


def parse_consolidation_plan(raw_json: str) -> ConsolidationPlan:
    """Parse curator JSON output into a validated ConsolidationPlan.

    Expected format:
    {
      "summary": "...",
      "actions": [
        {"action": "delete", "targets": ["file.md"], "reason": "..."},
        {"action": "merge", "targets": ["a.md", "b.md"], "new_content": "...", "new_filename": "merged.md"},
        {"action": "rewrite", "targets": ["c.md"], "new_content": "..."},
        {"action": "normalize_date", "targets": ["d.md"], "new_content": "..."}
      ]
    }
    """
    data = json.loads(extract_json_object(raw_json))
    if not isinstance(data, dict):
        raise ValueError("Plan must be a JSON object")

    actions: list[ConsolidationAction] = []
    for item in data.get("actions", []):
        action = item.get("action", "")
        if action not in VALID_ACTIONS:
            raise ValueError(f"Invalid action: {action!r}; must be one of {VALID_ACTIONS}")
        targets = item.get("targets", [])
        if not targets:
            raise ValueError(f"Action {action!r} has no targets")
        if action in ("merge", "rewrite", "normalize_date") and not item.get("new_content"):
            raise ValueError(f"Action {action!r} requires 'new_content'")
        if action == "merge" and not item.get("new_filename"):
            raise ValueError("'merge' action requires 'new_filename'")
        actions.append(ConsolidationAction(
            action=action,
            targets=targets,
            reason=item.get("reason", ""),
            new_content=item.get("new_content"),
            new_filename=item.get("new_filename"),
        ))

    return ConsolidationPlan(actions=actions, summary=data.get("summary", ""))


# ─── Backup ─────────────────────────────────────────────────

def _timestamp_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def create_backup(filenames: list[str]) -> str:
    """Back up specified memory files. Returns backup_id for rollback."""
    backup_id = _timestamp_id()
    backup_path = _backup_dir() / backup_id
    backup_path.mkdir(parents=True, exist_ok=True)

    mem_dir = project_memory_dir()
    for fn in filenames:
        src = mem_dir / fn
        if src.exists():
            shutil.copy2(str(src), str(backup_path / fn))

    # Write manifest
    manifest = {"backup_id": backup_id, "files": filenames, "created_at": backup_id}
    (backup_path / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    return backup_id


def rollback_backup(backup_id: str) -> list[str]:
    """Restore files from a backup. Returns list of restored filenames."""
    backup_path = _backup_dir() / backup_id
    if not backup_path.is_dir():
        raise FileNotFoundError(f"Backup {backup_id!r} not found")

    manifest_path = backup_path / "_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Backup manifest not found for {backup_id!r}")

    manifest = json.loads(manifest_path.read_text())
    mem_dir = project_memory_dir()
    restored: list[str] = []

    for fn in manifest["files"]:
        src = backup_path / fn
        if src.exists():
            shutil.copy2(str(src), str(mem_dir / fn))
            restored.append(fn)

    return restored


# ─── Archive (never hard-delete) ────────────────────────────

def archive_file(filename: str, reason: str = "") -> bool:
    """Move a memory file to the archive directory. Returns True if archived."""
    mem_dir = project_memory_dir()
    src = mem_dir / filename
    if not src.exists():
        return False

    archive = _archive_dir()
    ts = _timestamp_id()
    # Prefix with timestamp to avoid collisions
    dest = archive / f"{ts}_{filename}"
    shutil.move(str(src), str(dest))

    # Write a tiny metadata sidecar
    meta = {"original": filename, "archived_at": ts, "reason": reason}
    (archive / f"{ts}_{filename}.meta.json").write_text(json.dumps(meta))
    return True


# ─── Apply Consolidation Plan ──────────────────────────────

def apply_plan(plan: ConsolidationPlan) -> ConsolidationResult:
    """Apply a validated consolidation plan with backup + archive safety.

    Steps:
    1. Collect all target filenames across actions
    2. Create a backup of all targets
    3. Apply each action atomically per-file
    4. Archive deleted/merged sources
    """
    result = ConsolidationResult()
    mem_dir = project_memory_dir()

    # Collect all target files for backup
    all_targets: set[str] = set()
    for action in plan.actions:
        all_targets.update(action.targets)

    # Validate all targets exist
    missing = [fn for fn in all_targets if not (mem_dir / fn).exists()]
    if missing:
        result.errors.append(f"Missing files: {missing}")
        return result

    # Create backup
    result.backup_id = create_backup(list(all_targets))

    # Apply actions
    for action in plan.actions:
        try:
            if action.action == "delete":
                for fn in action.targets:
                    if archive_file(fn, reason=action.reason):
                        result.archived += 1
                    else:
                        result.errors.append(f"Failed to archive {fn}")

            elif action.action == "merge":
                # Archive all source files, write merged output
                for fn in action.targets:
                    if archive_file(fn, reason=f"merged into {action.new_filename}"):
                        result.merged += 1
                    else:
                        result.errors.append(f"Failed to archive {fn} during merge")
                # Write the merged file
                assert action.new_filename is not None
                assert action.new_content is not None
                (mem_dir / action.new_filename).write_text(action.new_content)

            elif action.action == "rewrite":
                for fn in action.targets:
                    assert action.new_content is not None
                    (mem_dir / fn).write_text(action.new_content)
                    result.rewritten += 1

            elif action.action == "normalize_date":
                for fn in action.targets:
                    assert action.new_content is not None
                    (mem_dir / fn).write_text(action.new_content)
                    result.normalized += 1

        except Exception as e:
            result.errors.append(f"Error applying {action.action} on {action.targets}: {e}")

    return result


# docs/22 Phase 7: the optimization config lifecycle moved out of markdown-land.
# The single retrieval-config truth source is now
# memory/retrieval_config_store.py at the SimpleMem store root
# ({store_root}/retrieval_config.json). The old markdown-centric
# load/save/rollback_evolve_config + evolve_config_path are deleted — no dual
# config truth source.


# ─── Optimization env knobs ─────────────────────────────────

# 人工最终决策覆盖 #1：confirmed 阈值默认 = 5（非 10）。小记忆库下 10 太难触发，
# 5 仍有调参意义。env NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED 始终可覆盖。
_DEFAULT_MIN_CONFIRMED = 5
_DEFAULT_MAX_ROUNDS = 7


def evolve_min_confirmed() -> int:
    """Minimum confirmed eval candidates required before optimize runs.

    env NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED overrides; invalid / <=0 -> default (5).
    """
    raw = os.environ.get("NANOCODE_MEMORY_EVOLVE_MIN_CONFIRMED")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MIN_CONFIRMED
    return v if v > 0 else _DEFAULT_MIN_CONFIRMED


def evolve_max_rounds() -> int:
    """Max EvolveMem optimization rounds.

    env NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS overrides; invalid / <=0 -> default (7).
    """
    raw = os.environ.get("NANOCODE_MEMORY_EVOLVE_MAX_ROUNDS")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROUNDS
    return v if v > 0 else _DEFAULT_MAX_ROUNDS


# ─── Eval Provenance Check ──────────────────────────────────

def prune_orphaned_evals(eval_dir: Path | None = None,
                         valid_refs: "set[str] | None" = None) -> int:
    """Remove eval entries whose source memories no longer exist.

    After consolidation/archival, some memories may be gone. Evals referencing
    them should be discarded so the optimizer doesn't tune on stale signals.

    Backend-aware (docs/22 Phase 2): when `valid_refs` is provided (e.g. from
    `eval_source.valid_memory_refs(backend)`), an eval is kept iff its full
    `source.memory_ref` is in that set — works for `simplemem://<id>` refs as
    well as markdown filenames. When `valid_refs` is None, falls back to the
    legacy markdown behavior (compare the `source_memory` basename against the
    project markdown files).

    Returns count of pruned entries.
    """
    if eval_dir is None:
        eval_dir = _simplemem_dir() / "eval"
    if not eval_dir.is_dir():
        return 0

    markdown_files = None
    if valid_refs is None:
        markdown_files = {f.name for f in project_memory_dir().glob("*.md") if f.name != "MEMORY.md"}
    pruned = 0

    for eval_file in eval_dir.glob("*.json"):
        try:
            data = json.loads(eval_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if valid_refs is not None:
            ref = ((data.get("source") or {}).get("memory_ref") or "").strip()
            if ref and ref not in valid_refs:
                eval_file.unlink()
                pruned += 1
            continue

        # Legacy markdown behavior: compare source_memory basename to .md files.
        source = data.get("source_memory", "")
        if source and source not in markdown_files:
            eval_file.unlink()
            pruned += 1

    return pruned


# ─── Curator Prompt (consolidation mode) ────────────────────

CURATOR_CONSOLIDATION_PROMPT = """You are a memory curator in CONSOLIDATION mode. Your job is to analyze the user's memory files and propose a cleanup plan.

You will be given the full contents of all memory files. Analyze them and produce a JSON plan with these possible actions:

## Actions you can propose:

1. **delete** — Remove obsolete/expired memories (they will be archived, not hard-deleted)
2. **merge** — Combine duplicate or highly overlapping memories into one
3. **rewrite** — Rewrite a memory to fix contradictions, update stale info, or improve clarity
4. **normalize_date** — Convert relative dates ("yesterday", "last week") to absolute dates

## Rules:
- Be CONSERVATIVE. Only propose changes you're confident about.
- Never delete memories that contain unique, potentially useful information.
- When merging, preserve ALL distinct information from both sources.
- When rewriting, keep the same frontmatter format (YAML with name, description, type).
- For date normalization, use ISO format (YYYY-MM-DD) in the content.

## Output format (strict JSON, nothing else):
{
  "summary": "Brief description of what this plan does",
  "actions": [
    {
      "action": "delete",
      "targets": ["filename.md"],
      "reason": "Why this should be deleted"
    },
    {
      "action": "merge",
      "targets": ["a.md", "b.md"],
      "new_filename": "merged_output.md",
      "new_content": "---\\nname: ...\\n---\\nMerged content...",
      "reason": "These are duplicates about X"
    },
    {
      "action": "rewrite",
      "targets": ["c.md"],
      "new_content": "---\\nname: ...\\n---\\nRewritten content...",
      "reason": "Fixed contradiction with current state"
    },
    {
      "action": "normalize_date",
      "targets": ["d.md"],
      "new_content": "---\\nname: ...\\n---\\nContent with absolute dates...",
      "reason": "Converted relative dates to absolute"
    }
  ]
}

If no cleanup is needed, return: {"summary": "No cleanup needed", "actions": []}

CRITICAL: Output ONLY the raw JSON object. Do NOT wrap it in markdown code fences (no ```json). Do NOT add any explanation before or after. Your entire response must start with { and end with }."""


def build_curator_user_message() -> str:
    """Build the user message for the curator: all memory file contents."""
    mem_dir = project_memory_dir()
    parts = [f"Today's date: {time.strftime('%Y-%m-%d')}", ""]
    parts.append("# Memory Files to Analyze\n")

    files = sorted(mem_dir.glob("*.md"))
    for f in files:
        if f.name == "MEMORY.md":
            continue
        try:
            content = f.read_text()
            parts.append(f"## File: {f.name}\n\n{content}\n\n")
        except OSError:
            continue

    if len(parts) <= 3:
        return "No memory files found. Return an empty plan."

    return "\n".join(parts)


# docs/22 Phase 2: the EVAL-mode curator input is now backend-aware and lives in
# memory/eval_source.py (build_eval_curator_message(backend)). The old
# markdown-only build_eval_curator_message() here is removed — no dual source.
